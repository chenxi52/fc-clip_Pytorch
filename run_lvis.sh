#!/bin/bash
#SBATCH --job-name=Edadet
#SBATCH --partition=vip1
#SBATCH --nodes=1
#SBATCH --cpus-per-task=128
#SBATCH --gres=gpu:8
#SBATCH --error=.output/slurm_out/runjob.%J.err
#SBATCH --output=.output/slurm_out/runjob.%J.out

# srun python train_net_stand.py --config-file configs/OpenDet_tiny_coco.yaml --num-gpus 8 \
#     OUTPUT_DIR output/clipRpn/SamOn/clip_mask_samPe_iouLoss MODEL.ROI_MASK_HEAD.IOU_LOSS_WEIGHT 1. &
srun python train_net_stand.py --config-file configs/OpenDet_tiny_lvis.yaml --num-gpus 8 \
    OUTPUT_DIR output/clipRpn/SamOn/twoFpn_lvis_MycontextFormer_FPNbn MODEL.FPN.NORM BN

# python train_net_stand.py --config-file configs/OpenDet_tiny_coco.yaml --num-gpus 6\
#     OUTPUT_DIR output/clipOn/SamOn/context_p \
#     SOLVER.IMS_PER_BATCH 18 \
#     SOLVER.BASE_LR 0.0005 \
