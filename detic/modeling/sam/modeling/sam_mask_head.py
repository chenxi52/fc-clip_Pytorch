import torch
import torch.nn as nn
from typing import Tuple, List, Dict
from detectron2.modeling import BaseMaskRCNNHead, ROI_MASK_HEAD_REGISTRY
from detectron2.config import configurable
from einops import repeat
from detectron2.structures import Instances, Boxes, BitMasks
import torch.nn.functional as F
from detectron2.utils.events import get_event_storage
from detectron2.layers import cat, batched_nms
from detectron2.layers.wrappers import move_device_like
from detic.modeling.ContextFormer import build_contextformer, build_my_contextFormer
from detectron2.modeling.roi_heads.fast_rcnn import _log_classification_stats
from detectron2.layers import nonzero_tuple
from fvcore.nn import sigmoid_focal_loss_jit
from detic.modeling.utils import load_class_freq, get_fed_loss_inds
import fvcore.nn.weight_init as weight_init
from detectron2.modeling.roi_heads.mask_head import mask_rcnn_loss
from torch.cuda.amp import autocast
import pickle
from detectron2.modeling.poolers import ROIPooler
from detectron2.layers import cross_entropy
from detic.data.datasets.coco_zeroshot import get_contigous_ids
from detic.data.datasets.lvis_v1_zeroshot import get_contigous_ids_lvis

@ROI_MASK_HEAD_REGISTRY.register()
class samMaskHead(BaseMaskRCNNHead):
    @configurable
    def __init__(
            self,
            vis_period: int = 0,
            with_sincos: bool = False,
            per_query_point: int = 4,
            clip_type: str = 'ViT-B/16',
            ignore_zero_cats: bool = False,
            text_feats: torch.Tensor = None,
            train_size: int = 224,
            add_pe_context: bool = False,
            cat_freq_path: str = None,
            fed_loss_freq_weight: float = 1.0,
            data_classes: int = 80,
            test_dataset_name: str = 'coco_2017_val',
            context_former_layer: str = 'DetrDecoderLayer',
            roi_prompter: str = 'CLIP',
            roi_prompter_fuse_type: str = 'add',
            **kwargs
            ) -> None:
        super().__init__(vis_period=vis_period)
        for name, value in locals().items():
            if name == 'self':
                continue
            elif name == 'kwargs':
                for kw_name, kw_value in value.items():
                    setattr(self, kw_name, kw_value)
            else:
                setattr(self, name, value)

        if with_sincos:
            sincos = 2
        else:
            sincos = 1
        
        # Prompt encoder
        in_channels = 256
        if roi_prompter=='FUSE' and roi_prompter_fuse_type=='stack':
            in_channels = in_channels * 2
        point_emb = nn.Sequential(
            nn.Conv2d(in_channels, 256, 3, stride=2, padding=1),
            nn.BatchNorm2d(256),
            nn.ReLU(inplace=True),
            nn.Flatten(),
            nn.Linear(7*7*256, 256),
            nn.ReLU(inplace=True),
            nn.Linear(256, 256),
            nn.ReLU(inplace=True),
            nn.Linear(256, 256*sincos*per_query_point)
        )
        self.point_emb = point_emb
        
        if clip_type == 'ViT-B/16':
            self.text_dim = 512
            self.emb_dim = 768
            self.down_dim = self.emb_dim
        elif clip_type == 'RN50':
            self.text_dim = 1024
            self.emb_dim = 2048
            self.down_dim = self.emb_dim
        elif clip_type == 'RN50x64':
            self.text_dim = 1024
            self.emb_dim = 4096
            self.down_dim = self.emb_dim
        self.contextformer = build_my_contextFormer(
            mask_dim=256,
            d_model=self.text_dim, 
            normalize_before=True,
            vis_dim=self.emb_dim,
            layer_type=context_former_layer
        )
        def init_weights(m):
            if type(m) == nn.Linear:
                weight_init.c2_xavier_fill(m)
            elif type(m) == nn.Conv2d:
                weight_init.c2_msra_fill(m)
        self.point_emb.apply(init_weights)
        self.contextformer.apply(init_weights)
        if ignore_zero_cats and 'coco' in test_dataset_name:
            base_ones = torch.zeros(len(get_contigous_ids('all'))) 
            base_ones[get_contigous_ids('seen')] = 1
            novel_ones = torch.zeros(len(get_contigous_ids('all')))
            novel_ones[get_contigous_ids('unseen')] = 1
            unused_index = get_contigous_ids('unused') # [0-79]
            self.register_buffer('unused_index', torch.tensor(unused_index))
        elif ignore_zero_cats and 'lvis' in test_dataset_name:
            base_ones = torch.zeros(len(get_contigous_ids_lvis('all')))
            base_ones[get_contigous_ids_lvis('seen')] = 1
            novel_ones = 1 - base_ones
        base_ones = torch.cat([base_ones, torch.ones(1)])
        self.register_buffer('base_ones', base_ones)
        novel_ones = torch.cat([novel_ones, torch.ones(1)])
        self.register_buffer('novel_ones', novel_ones)

        del self.text_feats
        self.register_buffer('text_feats', text_feats)
        if add_pe_context:
            self.contextformer_pe = nn.Parameter(torch.randn(1, (train_size//32)**2, self.contextformer.d_model), requires_grad=True)
        else:
            self.contextformer_pe = None
        if self.use_fed_loss or self.ignore_zero_cats:
            freq_weight = load_class_freq(cat_freq_path, fed_loss_freq_weight, data_classes)
            self.register_buffer('freq_weight', freq_weight)
        else:
            self.freq_weight = None
        
    @classmethod
    def from_config(cls, cfg, input_shape):
        if cfg.MODEL.ROI_MASK_HEAD.CLS_AGNOSTIC_MASK:
            num_classes = 1
        else:
            num_classes = cfg.MODEL.ROI_HEADS.NUM_CLASSES
        with open(cfg.MODEL.CLIP_TEXT_FEATS_PATH,'rb') as f:
            text_feats = pickle.load(f)
        test_pooler = ROIPooler(
            output_size=cfg.MODEL.ROI_BOX_HEAD.POOLER_RESOLUTION,
            scales=[1./32,],
            sampling_ratio=cfg.MODEL.ROI_BOX_HEAD.POOLER_SAMPLING_RATIO,
            pooler_type=cfg.MODEL.ROI_BOX_HEAD.POOLER_TYPE
        )
        return {'class_agnostic': cfg.MODEL.ROI_MASK_HEAD.CLS_AGNOSTIC_MASK,
                'per_query_point': cfg.MODEL.ROI_MASK_HEAD.PER_QUERY_POINT,
                'with_sincos': cfg.MODEL.ROI_MASK_HEAD.WITH_SINCOS,
                'train_size':  cfg.INPUT.TRAIN_SIZE,
                'num_classes': num_classes,
                'vis_period': cfg.VIS_PERIOD,
                'clip_type': cfg.MODEL.BACKBONE.TYPE,
                'score_thresh': cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST,
                'top_per_instance': cfg.TEST.DETECTIONS_PER_IMAGE,
                'test_nms_thresh': cfg.MODEL.ROI_HEADS.NMS_THRESH_TEST,
                'mask_loss_type': cfg.MODEL.ROI_MASK_HEAD.MASK_LOSS_TYPE,
                'data_classes': cfg.MODEL.ROI_HEADS.NUM_CLASSES,
                'test_score_type': cfg.TEST.SCORE_TYPE,
                'test_geometric_fact': cfg.TEST.GEOMETRIC_FACT,
                'mask_thr_binary': cfg.TEST.MASK_THR_BINARY,
                'mask_loss_weight':cfg.MODEL.ROI_MASK_HEAD.MASK_LOSS_WEIGHT,
                'text_feats': text_feats, 
                'test_pooler': test_pooler,
                'base_alpha': cfg.MODEL.ROI_BOX_HEAD.BASE_ALPHA,
                'novel_beta': cfg.MODEL.ROI_BOX_HEAD.NOVEL_BETA,
                'background_weight': cfg.MODEL.ROI_BOX_HEAD.BACKGROUND_WEIGHT,
                'eval_ar': cfg.EVAL_AR,
                'box_prompter': cfg.MODEL.ROI_MASK_HEAD.BOX_PROMPTER,
                'add_pe_context': cfg.MODEL.ROI_MASK_HEAD.ADD_PE_CONTEXT,

                'cat_freq_path': cfg.MODEL.ROI_BOX_HEAD.CAT_FREQ_PATH,
                'ignore_zero_cats': cfg.MODEL.ROI_BOX_HEAD.IGNORE_ZERO_CATS,
                'use_fed_loss': cfg.MODEL.ROI_BOX_HEAD.USE_FED_LOSS,
                'fed_loss_num_cat': cfg.MODEL.NUM_SAMPLE_CATS,
                'fed_loss_freq_weight': cfg.MODEL.ROI_BOX_HEAD.FED_LOSS_FREQ_WEIGHT,
                'add_position_emb': cfg.MODEL.ROI_MASK_HEAD.ADD_POSTTION_EMB,

                'iou_loss_weight': cfg.MODEL.ROI_MASK_HEAD.IOU_LOSS_WEIGHT,
                'use_iou_score': cfg.TEST.USE_IOU_SCORE,
                'test_dataset_name': cfg.DATASETS.TEST[0],
                'context_former_layer': cfg.MODEL.ROI_MASK_HEAD.CONTEXT_FORMER_LAYER,
                'roi_prompter': cfg.MODEL.ROI_MASK_HEAD.ROI_PROMPTER,
                'roi_prompter_fuse_type': cfg.MODEL.ROI_MASK_HEAD.ROI_PROMPTER_FUSE_TYPE,
                }
    
    def forward(
            self,
            roi_features: torch.Tensor,
            instances: List[Instances],
            sam: nn.Module,
            sam_features: torch.Tensor,
            clip_features: [torch.Tensor, Dict[str, torch.Tensor]],
            boxes: List[Boxes],
            attnpool: nn.Module = None,
            select_fore_cls: bool = True,
            box_prompter: str = 'Roi',
        ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        firstly, inference, and then calculate losses
        Args:
            roi_feature: sam features after maskroi, multi-level---> roi box, None when use boxPrompter
        Returns:
            A dict of losses in training. The predicted "instances" in inference(List[Dict['instances': Instances]]).
        """
        if roi_features is not None:
            batch_size = roi_features.shape[0]
            sparse_embeddings = self.point_emb(roi_features) #prompt head 
            sparse_embeddings = sparse_embeddings.view(batch_size, self.per_query_point, -1)
            if self.with_sincos and not self.add_position_emb: 
                sparse_embeddings = torch.sin(sparse_embeddings[..., ::2]) + sparse_embeddings[..., 1::2] #模拟sin+Emb
            elif self.add_position_emb and not self.with_sincos:
                assert NotImplementedError
                # sam_point_emb = sam.prompt_encoder.point_embeddings
                # point_labels = torch.ones()# postive points and negative points
                # # 1. negative points extraction
                # # 2. randomly sample background points, the least roi, or the least 
                # neg_points = sample_points(boxes.tensor, image_size=self.train_size, num_points=4)
                # point_emd[point_labels==1] += sam_point_emb[0].weight
                # point_emd[point_labels==0] += sam_point_emb[1].weight
            dense_embeddings = sam.prompt_encoder.no_mask_embed.weight.reshape(1, -1, 1, 1).expand(
                sparse_embeddings.shape[0], -1, *sam_features.shape[-2:]
            )
        else:
            sparse_embeddings = torch.empty((len(instances), 0, 256), device=boxes[0].device)
        if 'Box' in box_prompter: # box prompter
            # box prompter
            box_sparse_embeddings, box_dense_embeddings = sam.prompt_encoder(
                points= None,
                boxes = cat([b.tensor for b in boxes], dim=0),
                masks = None
            )
            sparse_embeddings = torch.cat([sparse_embeddings, box_sparse_embeddings], dim=1)
            
        clip_final_feats, _ = clip_features
        img_flag_ids = torch.tensor([len(i) for i in instances], device=clip_final_feats.device, dtype=torch.long)
        sam_features = torch.repeat_interleave(sam_features, img_flag_ids, dim=0)
        batch_clip_final_feats = torch.repeat_interleave(clip_final_feats, img_flag_ids, dim=0)
        
        img_pe = sam.prompt_encoder.get_dense_pe()
        img_pe = repeat(img_pe, 'b c h w -> (b n) c h w', n=sam_features.shape[0])
        
        # select foreGround proposals first will save computation here.
        with autocast():
            # box和 image features对应关系是，
            low_res_masks, mask_tokens, iou_outs = sam.mask_decoder.forward_batch(
                image_embeddings=sam_features,
                image_pe=img_pe,
                sparse_prompt_embeddings=sparse_embeddings,
                dense_prompt_embeddings=dense_embeddings,
                multimask_output=False,
            )
            logits_image = self.contextformer(
                mask_tokens, 
                batch_clip_final_feats, 
                self.text_feats, 
                pos=self.contextformer_pe if self.add_pe_context else None, 
                attention_mask=None)
            if len(logits_image.shape) > 2: #[bzs, n_tokens, dim]
                logits_image = logits_image.squeeze()

        low_res_masks = torch.nn.functional.interpolate(low_res_masks, size=(self.train_size, self.train_size), mode='bilinear', align_corners=False)
        if self.training:
            del boxes
            gt_classes = (cat([p.gt_classes for p in instances], dim=0) )
            assert len(logits_image.shape) == 2, print('the fore proposal is zero in this batch', logits_image.shape)
            # 当选前景 proposals 进入 mask head即 self.fore_mask_cls=True，这里 cls_accuracy=fg_cls_accuracy
            # if select_fore_cls=True, custom_mask_rcnn_loss should select foreground masks
            mask_loss, iou_loss = self.custom_mask_rcnn_loss(low_res_masks, instances, self.vis_period, select_fore_cls, iou_outs=iou_outs)
            loss ={"loss_mask": mask_loss * self.mask_loss_weight,
                   "loss_iou": iou_loss * self.iou_loss_weight,
                   "loss_cls": self.softmax_cross_entropy_loss(logits_image, gt_classes) * self.mask_loss_weight,
                   }
            return loss
        else:
            new_instances = self.custom_mask_rcnn_inference(pred_mask_logits = low_res_masks, 
                                                            pred_instances=instances, 
                                                            logits_image=logits_image, 
                                                            boxes=boxes,
                                                            clip_features=clip_final_feats,
                                                            attnpool=attnpool,
                                                            iou_pred=iou_outs)
            return new_instances
        

    def softmax_cross_entropy_loss(self, pred_class_logits, gt_classes):
        """
        change _no_instance handling
        """
        if pred_class_logits.numel() == 0:
            return pred_class_logits.new_zeros([1])[0]

        if self.ignore_zero_cats and (self.freq_weight is not None):
            zero_weight = torch.cat([
                (self.freq_weight.view(-1) > 1e-4).float(),
                self.freq_weight.new_ones(1)*self.background_weight]) # C + 1
            loss = F.cross_entropy(
                pred_class_logits, gt_classes, 
                weight=zero_weight, reduction="mean")
        elif self.use_fed_loss and (self.freq_weight is not None): # fedloss
            C = pred_class_logits.shape[1] - 1
            appeared = get_fed_loss_inds(
                gt_classes, 
                num_sample_cats=self.fed_loss_num_cat,
                C=C,
                weight=self.freq_weight)
            appeared_mask = appeared.new_zeros(C + 1).float()
            appeared_mask[appeared] = 1. # C + 1
            appeared_mask[C] = 1.
            loss = F.cross_entropy(
                pred_class_logits, gt_classes, 
                weight=appeared_mask, reduction="mean")        
        else:
            loss = F.cross_entropy(
                pred_class_logits, gt_classes, reduction="mean")   
            
        _log_classification_stats(pred_class_logits, gt_classes , 'fast_rcnn')
        return loss
    
    def focal_loss(self, inputs, targets, gamma=0.5, reduction="mean"):
        """Inspired by RetinaNet implementation"""
        if targets.numel() == 0 and reduction == "mean":
            return input.sum() * 0.0  # connect the gradient
        
        # focal scaling
        ce_loss = F.cross_entropy(inputs, targets, reduction="none")
        p = F.softmax(inputs, dim=-1)
        p_t = p[torch.arange(p.size(0)).to(p.device), targets]  # get prob of target class
        loss = ce_loss * ((1 - p_t) ** gamma)

        # bg loss weight
        if self.background_weight>0:
            loss_weight = torch.ones(loss.size(0)).to(p.device)
            loss_weight[targets == self.data_classes] = self.background_weight
            loss = loss * loss_weight

        if reduction == "mean":
            loss = loss.mean()

        return loss
    # @torch.jit.unused
    # def sigmoid_focal_loss(self, inputs, targets, gt_classes, alpha: float = 0.25, gamma: float = 2):
    #     """Compute the sigmoid focal loss."""
    #     _log_classification_stats(inputs, gt_classes, 'clip_fast_rcnn')
    #     prob = inputs.sigmoid()
    #     ce_loss = F.binary_cross_entropy_with_logits(inputs, targets, reduction="none")
    #     p_t = prob * targets + (1 - prob) * (1 - targets)
    #     loss = ce_loss * ((1 - p_t) ** gamma)
    #     B = inputs.shape[0]
    #     C = inputs.shape[1] - 1
    #     weight = 1
    #     if alpha >= 0:
    #         loss = (alpha * targets + (1 - alpha) * (1 - targets)) * loss
    #     # if use fed_loss, the background is not sampled ?
    #     if self.use_fed_loss and (self.freq_weight is not None): # fedloss
    #         appeared = get_fed_loss_inds(
    #             gt_classes, 
    #             num_sample_cats=self.fed_loss_num_cat,
    #             C=C,
    #             weight=self.freq_weight)
    #         appeared_mask = appeared.new_zeros(C + 1)
    #         appeared_mask[appeared] = 1 # C + 1
    #         weight = appeared_mask.float() # 
    #     if self.ignore_zero_cats and (self.freq_weight is not None):
    #         w = (self.freq_weight.view(-1) > 1e-4).float()
    #         w = torch.cat([w, w.new_ones(1)])
    #         weight = weight * w
    #     return (loss*weight).mean(1).sum() / B
    
    @torch.jit.unused
    def custom_mask_rcnn_loss(self, pred_mask_logits: torch.Tensor, 
                              instances: List[Instances], 
                              vis_period: int = 0,
                              select_fore_cls: bool = False,
                              iou_outs: torch.Tensor = None):
        """
        remove gt_masks.crop_and_resize from original mask_rcnn_loss 
        with foreground selection
        """
        cls_agnostic_mask = pred_mask_logits.size(1) == 1
        gt_masks = []
        pred_mask_list = []
        iou_pred_list = []
        pred_mask_per_logits = pred_mask_logits.split([len(x) for x in instances], dim=0)
        iou_outs = iou_outs.split([len(x) for x in instances], dim=0)
        for instances_per_image, pred_logits_per_image, iou_out_per_image in zip(instances, pred_mask_per_logits, iou_outs):
            if len(instances_per_image) == 0:
                continue
            gt_classes_per_image = instances_per_image.gt_classes.to(dtype=torch.int64)
            ######因为之前并没有选出前景的 propsals做分类， 这里应该选出前景的masks
            if not select_fore_cls: 
                fg_inds = nonzero_tuple((gt_classes_per_image >= 0) & (gt_classes_per_image < self.data_classes))[0]
                if len(fg_inds) == 0:
                    continue
                gt_masks_per_image = instances_per_image[fg_inds].gt_masks.tensor
                pred_mask_list.append(pred_logits_per_image[fg_inds])
                iou_pred_list.append(iou_out_per_image[fg_inds])
                # A tensor of shape (N, M, M), N=#instances in the image; M=mask_side_len
            else:
                gt_masks_per_image = instances_per_image.gt_masks.tensor
                pred_mask_list.append(pred_logits_per_image)
                iou_pred_list.append(iou_out_per_image)
            gt_masks_per_image = F.pad(gt_masks_per_image, (0, self.train_size-gt_masks_per_image.shape[-1], 0, self.train_size-gt_masks_per_image.shape[-2]), value=0)
            gt_masks.append(gt_masks_per_image)
        ###########
        if len(gt_masks) == 0:
            return pred_mask_logits.sum() * 0
        gt_masks = cat(gt_masks, dim=0)
        pred_mask_logits = cat(pred_mask_list, dim=0)
        iou_preds = cat(iou_pred_list, dim=0)
        if cls_agnostic_mask:
            pred_mask_logits = pred_mask_logits[:, 0]
        else:
            assert NotImplementedError
            # assert len(gt_classes)>0, print('gt_classes is empty when cls_agnostic_mask = False')
            # indices = torch.arange(total_num_masks)
            # gt_classes = cat(gt_classes, dim=0)
            # pred_mask_logits = pred_mask_logits[indices, gt_classes]
        if gt_masks.dtype == torch.bool:
            gt_masks_bool = gt_masks
        else:
            # Here we allow gt_masks to be float as well (depend on the implementation of rasterize())
            gt_masks_bool = gt_masks > 0.5
        gt_masks = gt_masks.to(dtype=torch.float32)

        # Log the training accuracy (using gt classes and sigmoid(0.0) == 0.5 threshold)
        mask_incorrect = (pred_mask_logits > self.mask_thr_binary) != gt_masks_bool
        mask_accuracy = 1 - (mask_incorrect.sum().item() / max(mask_incorrect.numel(), 1.0))
        num_positive = gt_masks_bool.sum().item()
        false_positive = (mask_incorrect & ~gt_masks_bool).sum().item() / max(
            gt_masks_bool.numel() - num_positive, 1.0
        )
        false_negative = (mask_incorrect & gt_masks_bool).sum().item() / max(num_positive, 1.0)

        storage = get_event_storage()
        storage.put_scalar("mask_rcnn/accuracy", mask_accuracy)
        storage.put_scalar("mask_rcnn/false_positive", false_positive)
        storage.put_scalar("mask_rcnn/false_negative", false_negative)
        if vis_period > 0 and storage.iter % vis_period == 0:
            pred_masks = pred_mask_logits.sigmoid()
            pred_masks_thre = pred_masks > self.mask_thr_binary
            vis_masks = torch.cat([pred_masks, pred_masks_thre, gt_masks], axis=2)
            name = "Left: mask prediction;   Middle: thre0.5 ;Right: mask GT"
            for idx, vis_mask in enumerate(vis_masks):
                vis_mask = torch.stack([vis_mask] * 3, axis=0)
                storage.put_image(name, vis_mask)
                break
                
        if self.mask_loss_type == 'ce':
            mask_loss = F.binary_cross_entropy_with_logits(pred_mask_logits, gt_masks, reduction="mean")
        elif self.mask_loss_type == 'focal_dice':
            # suitable for open-vocabulary setting
            focalLoss = sigmoid_focal_loss_jit(pred_mask_logits, 
                                            gt_masks,
                                            alpha=0.25,
                                            gamma=2.0,
                                            reduction="mean")
            diceLoss = dice_loss(pred_mask_logits,
                                gt_masks)
            mask_loss = focalLoss + diceLoss
        elif self.mask_loss_type == 'ce_dice':
            ceLoss = F.binary_cross_entropy_with_logits(pred_mask_logits, gt_masks, reduction="mean")
            diceLoss, dice = dice_loss(pred_mask_logits,gt_masks,return_dice=True)
            mask_loss = ceLoss + diceLoss
            if self.iou_loss_weight > 0:
                assert iou_outs is not None
                iou_preds = iou_preds.squeeze(1)
                iouLoss = torch.nn.BCEWithLogitsLoss(reduction="mean")(iou_preds, dice)
            else: 
                iouLoss = diceLoss.new_zeros(1)[0]
        else:
            assert False, 'mask loss type not supported'
        return mask_loss, iouLoss

    def custom_mask_rcnn_inference(self, 
                                pred_mask_logits: torch.Tensor, 
                                pred_instances: List[Instances], 
                                logits_image: torch.Tensor,
                                boxes: List[Boxes],
                                clip_features: torch.Tensor = None,
                                attnpool: nn.AdaptiveAvgPool2d = None,
                                iou_pred: torch.Tensor = None
                                ):
        """
        boxes to crop vlm features and get vlm_scores
        """
        cls_agnostic_mask = pred_mask_logits.size(1) == 1
        if cls_agnostic_mask:
            mask_probs_pred = pred_mask_logits.sigmoid()
        else:
            # Select masks corresponding to the predicted classes
            num_masks = pred_mask_logits.shape[0]
            class_pred = cat([i.pred_classes for i in pred_instances])
            device = (
                class_pred.device
                if torch.jit.is_scripting()
                else ("cpu" if torch.jit.is_tracing() else class_pred.device)
            )
            indices = move_device_like(torch.arange(num_masks, device=device), class_pred)
            mask_probs_pred = pred_mask_logits[indices, class_pred][:, None].sigmoid()
        num_boxes_per_image = [len(i) for i in pred_instances]
        
        vlm_box_features = self.test_pooler([clip_features], boxes)
        # vlm pooler layer: clip attenpool
        vlm_box_features = attnpool(vlm_box_features)
        vlm_box_features = vlm_box_features / vlm_box_features.norm(dim=1, keepdim=True)
        logits_scale = 1/0.01
        vlm_scores = logits_scale * vlm_box_features @ (self.text_feats.t().to(vlm_box_features.device))

        vlm_scores = vlm_scores.split(num_boxes_per_image, dim=0)
        logits_image = logits_image.split(num_boxes_per_image, dim=0)
        mask_probs_pred = mask_probs_pred.split(num_boxes_per_image, dim=0)
        iou_pred = iou_pred.split(num_boxes_per_image, dim=0)
        results = [self.inference_single_image(mask_preds_per_img, 
                                           scores_ori=scores_per_img, 
                                           instances=instances_per_img,
                                           vlm_scores_ori=vlm_scores_per_img,
                                           iou_pred=iou_pred_per_img) 
                    for vlm_scores_per_img, scores_per_img, mask_preds_per_img, instances_per_img, iou_pred_per_img in 
                         zip(vlm_scores, logits_image, mask_probs_pred, pred_instances, iou_pred)]
        return results

    # classification score consider vlm text score.
    def inference_single_image(self, mask_pred, scores_ori, instances, vlm_scores_ori, iou_pred):
        vlm_scores = vlm_scores_ori.clone()
        scores = scores_ori.clone()
        if self.use_iou_score: 
            scores = scores * iou_pred.sigmoid()
        if hasattr(self, 'unsed_index'):
            scores[:, self.unused_index] = float('-inf')
            vlm_scores[:, self.unused_index] = float('-inf')
        vlm_scores = F.softmax(vlm_scores, dim=1)
        scores = F.softmax(scores, dim=1)

        new_instance = Instances(instances.image_size).to(scores.device)
        boxes = instances.pred_boxes.tensor
        objectness = instances.objectness
        if self.test_score_type == 'ob_mul_cls':
            assert NotImplementedError
            ensembled_socres = scores * objectness[:, None]
        elif self.test_score_type == 'ob_geo_cls':
            ensembled_socres = scores**(1-self.test_geometric_fact) * objectness[:, None]**self.test_geometric_fact
            ensembled_socres = ensembled_socres / ensembled_socres.sum(dim=1, keepdim=True)
            ensembled_socres = ensembled_socres[:, :-1]
            assert ensembled_socres[:, self.unused_index].max() < 1e-5, 'unused classes should not be evaluated'
        elif self.test_score_type == 'cls':
            # with vlm scores
            base_score = ((scores * self.base_ones)**(1-self.base_alpha)) * ((vlm_scores*self.base_ones)**(self.base_alpha))
            novel_score = ((scores * self.novel_ones)**(1-self.novel_beta)) * ((vlm_scores * self.novel_ones)**(self.novel_beta))
            ensembled_socres = base_score + novel_score
            # use detection 
            ensembled_socres = torch.cat([ensembled_socres[:,:-1], scores[:,-1:]], dim=1)
            ensembled_socres = ensembled_socres / ensembled_socres.sum(dim=1, keepdim=True)
            ensembled_socres = ensembled_socres[:, :-1]
            if hasattr(self, 'unsed_index'):
                assert ensembled_socres[:, self.unused_index].max() < 1e-5, 'unused classes should not be evaluated'

        filter_mask = ensembled_socres > self.score_thresh
        num_bbox_reg_classes = boxes.shape[1] // 4
        filter_inds = filter_mask.nonzero()
        boxes = boxes.view(-1, num_bbox_reg_classes, 4)
        if num_bbox_reg_classes == 1:
            boxes = boxes[filter_inds[:, 0], 0]
        else:
            boxes = boxes[filter_mask]
        ensembled_socres = ensembled_socres[filter_mask]
        keep = batched_nms(boxes, ensembled_socres, filter_inds[:, 1], self.test_nms_thresh)
        if self.top_per_instance >= 0:
            keep = keep[:self.top_per_instance]
        boxes, ensembled_socres, filter_inds = boxes[keep], ensembled_socres[keep], filter_inds[keep]

        new_instance.pred_boxes = Boxes(boxes)  # (1, Hmask, Wmask)
        new_instance.scores = ensembled_socres
        new_instance.pred_classes = filter_inds[:,1]
        new_instance.pred_masks = mask_pred[filter_inds[:,0]]
        if self.eval_ar:
            new_instance.objectness_logits = objectness[filter_inds[:,0]]
        return new_instance

@torch.jit.unused
def dice_loss(pred,
            target,
            weight=None,
            eps=1e-3,
            reduction='mean',
            avg_factor=None,
            return_dice=False):
    """
    Args:
        pred (torch.Tensor): The prediction, has a shape (n, *)
        target (torch.Tensor): The learning label of the prediction,
            shape (n, *), same shape of pred.
        weight (torch.Tensor, optional): The weight of loss for each
            prediction, has a shape (n,). Defaults to None.
        eps (float): Avoid dividing by zero. Default: 1e-3.
        reduction (str, optional): The method used to reduce the loss into
            a scalar. Defaults to 'mean'.
            Options are "none", "mean" and "sum".
        avg_factor (int, optional): Average factor that is used to average
            the loss. Defaults to None.
    """
    input = pred.sigmoid().flatten(1)
    target = target.flatten(1).float()
    a = torch.sum(input * target, 1)
    b = torch.sum(input * input, 1) + eps
    c = torch.sum(target * target, 1) + eps
    d = (2 * a) / (b + c)
    loss = 1 - d
    if weight is not None:
        assert weight.ndim == loss.ndim
        assert len(weight) == len(pred)
    loss = weight_reduce_loss(loss, weight, reduction, avg_factor)
    if return_dice:
        return loss,d
    return loss

@torch.jit.unused
def weight_reduce_loss(loss, weight=None, reduction='mean', avg_factor=None):
    # if weight is specified, apply element-wise weight
    if weight is not None:
        loss = loss * weight

    # if avg_factor is not specified, just reduce the loss
    if avg_factor is None:
        loss = reduce_loss(loss, reduction)
    else:
        # if reduction is mean, then average the loss by avg_factor
        if reduction == 'mean':
            loss = loss.sum() / avg_factor
        elif reduction != 'none':
            raise ValueError('avg_factor can not be used with reduction="sum"')
    return loss
@torch.jit.unused
def reduce_loss(loss, reduction):
    reduction_enum = F._Reduction.get_enum(reduction)
    # none: 0, elementwise_mean:1, sum: 2
    if reduction_enum == 0:
        return loss
    elif reduction_enum == 1:
        return loss.mean()
    elif reduction_enum == 2:
        return loss.sum()

def sample_points(boxes, image_size, num_points=5):
    # Expand the boxes by 1 pixel
    expanded_boxes = boxes.clone()
    expanded_boxes[:, :2] -= 1  # left top point
    expanded_boxes[:, 2:] += 1  # right down point

    # Ensure the expanded boxes do not exceed the image size
    expanded_boxes[:, :2].clamp_(min=0)
    expanded_boxes[:, 2:].clamp_(max=image_size)

    # Sample points in the expanded boxes
    sampled_points = torch.rand((num_points, 2)) * (expanded_boxes[:, 2:] - expanded_boxes[:, :2]) + expanded_boxes[:, :2]

    # Check if the sampled points are on the border of the original boxes
    on_border = (sampled_points == boxes[:, :2]) | (sampled_points == boxes[:, 2:])

    # If a point is on the border of the original box, resample it
    while on_border.any():
        resample_indices = on_border.any(dim=-1)
        new_points = torch.rand((resample_indices.sum(), 2)) * (expanded_boxes[resample_indices, 2:] - expanded_boxes[resample_indices, :2]) + expanded_boxes[resample_indices, :2]
        sampled_points[resample_indices] = new_points
        on_border = (sampled_points == boxes[:, :2]) | (sampled_points == boxes[:, 2:])

    return sampled_points