#!/bin/bash
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=1
#SBATCH --gpus-per-node=4
#SBATCH --output=result/spg_result_%j.txt
#SBATCH --error=error/spg_hicodetOD_%j.txt
#SBATCH --mem=256G
#SBATCH --time=12:00:00
#SBATCH --job-name=mmlab_example
#SBATCH --account=PCS0269

# module load python/3.9
# module load cuda/11.6.2

# source ~/ascebd_env/bin/activate
source activate pocket
cd ~/spatially-conditioned-graphs

CUDA_LAUNCH_BLOCKING=1  python configures/hicodet/adamixer_transH_spatial_r50_main.py --world-size 4 --learning-rate  0.0001 --cache-dir checkpoints/hicodet/new_spatial/transh_real_70 \
--num-workers 4 --num-epochs 12