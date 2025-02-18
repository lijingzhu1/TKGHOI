"""
Train and validate with distributed data parallel based on SCG
"""

import torch
import torchvision.ops.boxes as box_ops
import mmcv
from torch import nn, Tensor
from torchvision.ops._utils import _cat
from typing import Optional, List, Tuple
from collections import OrderedDict
from torchvision.ops import MultiScaleRoIAlign
from torchvision.models.detection import transform

import pocket.models as models
from mmcv.runner import load_checkpoint
from mmdet.models import build_detector

from mmdet.utils import get_root_logger, get_device
logger = get_root_logger()
import sys
sys.path.append('/users/PCS0256/lijing/spatially-conditioned-graphs')
from transforms import HOINetworkTransform
sys.path.append('/users/PCS0256/lijing/spatially-conditioned-graphs/heads')
from adamixer_transH_spatial_r50_head import InteractionHead, GraphHead

class GenericHOINetwork(nn.Module):
    """A generic architecture for HOI classification

    Parameters:
    -----------
        backbone: nn.Module
        interaction_head: nn.Module
        transform: nn.Module
        postprocess: bool
            If True, rescale bounding boxes to original image size
    """
    def __init__(self,
         detector_backbone:nn.Module,detector_neck: nn.Module,interaction_head: nn.Module,
        transform: nn.Module, postprocess: bool = True
    ) -> None:
        super().__init__()
        self.detector_backbone = detector_backbone
        self.detector_neck = detector_neck
        self.interaction_head = interaction_head
        self.transform = transform

        self.postprocess = postprocess


    def preprocess(self,
        images: List[Tensor],
        detections: List[dict],
        targets: Optional[List[dict]] = None
    ) -> Tuple[
        List[Tensor], List[dict],
        List[dict], List[Tuple[int, int]]
    ]:
        original_image_sizes = [img.shape[-2:] for img in images]
        images, targets = self.transform(images, targets)

        for det, o_im_s, im_s in zip(
            detections, original_image_sizes, images.image_sizes
        ):
            boxes = det['boxes']
            boxes = transform.resize_boxes(boxes, o_im_s, im_s)
            det['boxes'] = boxes

        return images, detections, targets, original_image_sizes

    def forward(self,
        images: List[Tensor],
        detections: List[dict],
        targets: Optional[List[dict]] = None
    ) -> List[dict]:
        """
        Parameters:
        -----------
            images: List[Tensor]
            detections: List[dict]
            targets: List[dict]

        Returns:
        --------
            results: List[dict]
        """
        if self.training and targets is None:
            raise ValueError("In training mode, targets should be passed")

        images, detections, targets, original_image_sizes = self.preprocess(
                images, detections, targets)

        features = self.detector_backbone(images.tensors)
        features = self.detector_neck(features)
        # logger.info(f'features:{features[0].size()},{features[1].size()},{features[2].size()},{features[3].size()}')
        # logger.info(f'nan or not in features:{torch.any(torch.isnan(features[0]))},{torch.any(torch.isnan(features[1]))},{torch.any(torch.isnan(features[2]))},{torch.any(torch.isnan(features[3]))}')
        box_feature = OrderedDict()
        box_feature['0'] = features[0]
        box_feature['1'] = features[1]
        box_feature['2'] = features[2]
        box_feature['3'] = features[3]
        # logger.info(f'box_feature:{[(key, value.size()) for key,value in box_feature.items()]}')
        results = self.interaction_head(box_feature, detections, 
            images.image_sizes, targets)

        if self.postprocess and results is not None:
            return self.transform.postprocess(
                results,
                images.image_sizes,
                original_image_sizes
            )
        else:
            return results

class SpatiallyConditionedGraph(GenericHOINetwork):
    def __init__(self,
        object_to_action: List[list],
        human_idx: int,
        # Backbone parameters
        backbone_name: str = "resnet50",
        pretrained: bool = True,
        # Pooler parameters
        output_size: int = 7,
        sampling_ratio: int = 2,
        # Box pair head parameters
        node_encoding_size: int = 1024,
        representation_size: int = 1024,
        num_classes: int = 117,
        box_score_thresh: float = 0.2,
        fg_iou_thresh: float = 0.5,
        num_iterations: int = 2,
        distributed: bool = False,
        # Transformation parameters
        min_size: int = 800, max_size: int = 1333,
        image_mean: Optional[List[float]] = None,
        image_std: Optional[List[float]] = None,
        postprocess: bool = True,
        # Preprocessing parameters
        box_nms_thresh: float = 0.5,
        max_human: int = 15,
        max_object: int = 15
    ) -> None:

        config = '/users/PCS0256/lijing/mmdetection_ascend/checkpoints/hoi_adamixer/adamixer_finetuning_r50_36_epoch/r50_36_epoch_finetuning_hicodet.py'
        checkpoint = '/users/PCS0256/lijing/mmdetection_ascend/checkpoints/hoi_adamixer/adamixer_finetuning_r50_36_epoch/epoch_11.pth'

        cfg = mmcv.Config.fromfile(config)
        # detector = build_detector(cfg.model, train_cfg = cfg.get('train_cfg'), test_cfg=cfg.get('test_cfg'))
        detector = build_detector(cfg['model'])

        if checkpoint is not None:
            checkpoint = load_checkpoint(detector, checkpoint, map_location='cpu')
        # detector = models.fasterrcnn_resnet_fpn(backbone_name,
        #     pretrained=pretrained)
        # backbone = detector.backbone
        detector_backbone = detector.backbone
        detector_neck = detector.neck
        box_roi_pool = MultiScaleRoIAlign(
            featmap_names=['0', '1', '2', '3'],
            output_size=output_size,
            sampling_ratio=sampling_ratio
        )

        box_pair_head = GraphHead(
            out_channels=detector_neck.out_channels,
            roi_pool_size=output_size,
            node_encoding_size=node_encoding_size,
            representation_size=representation_size,
            num_cls=num_classes,
            human_idx=human_idx,
            object_class_to_target_class=object_to_action,
            fg_iou_thresh=fg_iou_thresh,
            num_iter=num_iterations
        )

        box_pair_predictor = nn.Linear(representation_size * 2, num_classes)
        box_pair_suppressor = nn.Linear(representation_size * 2, 1)

        interaction_head = InteractionHead(
            box_roi_pool=box_roi_pool,
            box_pair_head=box_pair_head,
            box_pair_suppressor=box_pair_suppressor,
            box_pair_predictor=box_pair_predictor,
            num_classes=num_classes,
            human_idx=human_idx,
            box_nms_thresh=box_nms_thresh,
            box_score_thresh=box_score_thresh,
            max_human=max_human,
            max_object=max_object,
            distributed=distributed
        )

        if image_mean is None:
            image_mean = [0.485, 0.456, 0.406]
        if image_std is None:
            image_std = [0.229, 0.224, 0.225]
        transform = HOINetworkTransform(min_size, max_size,
            image_mean, image_std)

        super().__init__(detector_backbone,detector_neck, interaction_head, transform, postprocess)
