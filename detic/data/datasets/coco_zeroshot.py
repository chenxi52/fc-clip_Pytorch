# Copyright (c) Facebook, Inc. and its affiliates.
import os

from detectron2.data.datasets.register_coco import register_coco_instances
from detectron2.data.datasets.builtin_meta import _get_builtin_metadata
from detectron2.data import MetadataCatalog
# 48 base classes
categories_seen = [
    {'id': 1, 'name': 'person'},
    {'id': 2, 'name': 'bicycle'},
    {'id': 3, 'name': 'car'},
    {'id': 4, 'name': 'motorcycle'},
    {'id': 7, 'name': 'train'},
    {'id': 8, 'name': 'truck'},
    {'id': 9, 'name': 'boat'},
    {'id': 15, 'name': 'bench'},
    {'id': 16, 'name': 'bird'},
    {'id': 19, 'name': 'horse'},
    {'id': 20, 'name': 'sheep'},
    {'id': 23, 'name': 'bear'},
    {'id': 24, 'name': 'zebra'},
    {'id': 25, 'name': 'giraffe'},
    {'id': 27, 'name': 'backpack'},
    {'id': 31, 'name': 'handbag'},
    {'id': 33, 'name': 'suitcase'},
    {'id': 34, 'name': 'frisbee'},
    {'id': 35, 'name': 'skis'},
    {'id': 38, 'name': 'kite'},
    {'id': 42, 'name': 'surfboard'},
    {'id': 44, 'name': 'bottle'},
    {'id': 48, 'name': 'fork'},
    {'id': 50, 'name': 'spoon'},
    {'id': 51, 'name': 'bowl'},
    {'id': 52, 'name': 'banana'},
    {'id': 53, 'name': 'apple'},
    {'id': 54, 'name': 'sandwich'},
    {'id': 55, 'name': 'orange'},
    {'id': 56, 'name': 'broccoli'},
    {'id': 57, 'name': 'carrot'},
    {'id': 59, 'name': 'pizza'},
    {'id': 60, 'name': 'donut'},
    {'id': 62, 'name': 'chair'},
    {'id': 65, 'name': 'bed'},
    {'id': 70, 'name': 'toilet'},
    {'id': 72, 'name': 'tv'},
    {'id': 73, 'name': 'laptop'},
    {'id': 74, 'name': 'mouse'},
    {'id': 75, 'name': 'remote'},
    {'id': 78, 'name': 'microwave'},
    {'id': 79, 'name': 'oven'},
    {'id': 80, 'name': 'toaster'},
    {'id': 82, 'name': 'refrigerator'},
    {'id': 84, 'name': 'book'},
    {'id': 85, 'name': 'clock'},
    {'id': 86, 'name': 'vase'},
    {'id': 90, 'name': 'toothbrush'},
]
#17 classes
categories_unseen = [
    {'id': 5, 'name': 'airplane'},
    {'id': 6, 'name': 'bus'},
    {'id': 17, 'name': 'cat'},
    {'id': 18, 'name': 'dog'},
    {'id': 21, 'name': 'cow'},
    {'id': 22, 'name': 'elephant'},
    {'id': 28, 'name': 'umbrella'},
    {'id': 32, 'name': 'tie'},
    {'id': 36, 'name': 'snowboard'},
    {'id': 41, 'name': 'skateboard'},
    {'id': 47, 'name': 'cup'},
    {'id': 49, 'name': 'knife'},
    {'id': 61, 'name': 'cake'},
    {'id': 63, 'name': 'couch'},
    {'id': 76, 'name': 'keyboard'},
    {'id': 81, 'name': 'sink'},
    {'id': 87, 'name': 'scissors'},
]


def _get_metadata(cat):
    if cat == 'all':
        return _get_builtin_metadata('coco')
    elif cat == 'seen':
        id_to_name = {x['id']: x['name'] for x in categories_seen}
    else:
        assert cat == 'unseen'
        id_to_name = {x['id']: x['name'] for x in categories_unseen}
    
    thing_dataset_id_to_contiguous_id = {
        x: i for i, x in enumerate(sorted(id_to_name))}
    # 只是 all 的话就是
    thing_classes = [id_to_name[k] for k in sorted(id_to_name)]
    return {
        "thing_dataset_id_to_contiguous_id": thing_dataset_id_to_contiguous_id,
        "thing_classes": thing_classes}


_PREDEFINED_SPLITS_COCO = {
    "coco_zeroshot_train": ("coco/train2017", "coco/zero-shot/instances_train2017_seen_2.json", 'seen'),
    "coco_zeroshot_val": ("coco/val2017", "coco/zero-shot/instances_val2017_unseen_2.json", 'unseen'),
    "coco_not_zeroshot_val": ("coco/val2017", "coco/zero-shot/instances_val2017_seen_2.json", 'seen'),
    "coco_generalized_zeroshot_val": ("coco/val2017", "coco/zero-shot/instances_val2017_all_2_oriorder.json", 'all'),
    "coco_zeroshot_train_oriorder": ("coco/train2017", "coco/zero-shot/instances_train2017_seen_2_oriorder.json", 'all'),
}
_root = os.getenv("DETECTRON2_DATASETS", "datasets")

for key, (image_root, json_file, cat) in _PREDEFINED_SPLITS_COCO.items():
    register_coco_instances(
        key,
        _get_metadata(cat),
        os.path.join(_root, json_file) if "://" not in json_file else json_file,
        os.path.join(_root, image_root),
    )

def get_contigous_ids(cat):
    # 直接输出 相对于 80 类别的continuous id, 
    if cat == 'all':
        return list(range(80))
    elif cat == 'seen':
        id_to_name = {x['id']: x['name'] for x in categories_seen}
    elif cat == 'unseen':
        id_to_name = {x['id']: x['name'] for x in categories_unseen}
    elif cat == 'seen_unseen' or cat == 'unused':
        id_to_name = categories_seen + categories_unseen
        id_to_name = {x['id']: x['name'] for x in id_to_name}
    thing_dataset_id_to_contiguous_id = _get_metadata('all')["thing_dataset_id_to_contiguous_id"]
    contiguous_ids = [thing_dataset_id_to_contiguous_id[x] for x in id_to_name.keys()]
    if cat == 'unused':
        contiguous_ids = list(set(range(80)) - set(contiguous_ids))
    return contiguous_ids
