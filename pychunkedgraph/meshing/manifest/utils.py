from time import time
from typing import List
from typing import Dict
from typing import Tuple
from typing import Sequence

import numpy as np
from cloudfiles import CloudFiles
from cloudvolume import CloudVolume

from ..meshgen_utils import get_mesh_name
from ..meshgen_utils import get_json_info
from ...graph import ChunkedGraph
from ...graph.types import empty_1d
from ...graph.utils.basetypes import NODE_ID
from ...graph.utils import generic as misc_utils


def _del_none_keys(d: dict):
    none_keys = []
    d_new = dict(d)
    for k, v in d.items():
        if v:
            continue
        none_keys.append(k)
        del d_new[k]
    return d_new, none_keys


def _get_children(cg, node_ids: Sequence[np.uint64], children_cache: dict = {}):
    """
    Helper function that makes use of cache.
    `_check_skips` also needs to know about children so cache is shared between them.
    """
    if not len(node_ids):
        return empty_1d.copy()
    node_ids = np.array(node_ids, dtype=NODE_ID)
    mask = np.in1d(node_ids, np.fromiter(children_cache.keys(), dtype=NODE_ID))
    children_d = cg.get_children(node_ids[~mask])
    children_cache.update(children_d)

    children = [empty_1d]
    for id_ in node_ids:
        children.append(children_cache[id_])
    return np.concatenate(children)


def _get_initial_meshes(
    cg,
    shard_readers,
    node_ids: Sequence[np.uint64],
    stop_layer: int = 2,
) -> Dict:
    children_cache = {}
    result = {}
    if not len(node_ids):
        return result
    node_layers = cg.get_chunk_layers(node_ids)
    stop_layer_ids = [node_ids[node_layers == stop_layer]]
    while np.any(node_layers > stop_layer):
        stop_layer_ids.append(node_ids[node_layers == stop_layer])
        ids_ = node_ids[node_layers > stop_layer]
        ids_, skips = check_skips(cg, ids_, children_cache=children_cache)

        start = time()
        result_ = shard_readers.initial_exists(ids_, return_byte_range=True)
        result_, missing_ids = _del_none_keys(result_)
        result.update(result_)
        print("ids, missing", ids_.size, len(missing_ids), time() - start)

        node_ids = _get_children(cg, missing_ids, children_cache=children_cache)
        node_ids = np.concatenate([node_ids, skips])
        node_layers = cg.get_chunk_layers(node_ids)

    # remainder IDs
    start = time()
    stop_layer_ids = np.concatenate(
        [*stop_layer_ids, node_ids[node_layers == stop_layer]]
    )
    # result_ = shard_readers.readers[stop_layer].exists(
    #     labels=stop_layer_ids, path=f"{mesh_dir}/initial/{stop_layer}/", return_byte_range=True,
    # )
    result_ = shard_readers.initial_exists(stop_layer_ids, return_byte_range=True)
    print(f"{stop_layer}:{stop_layer_ids.size} {time()-start}")
    result_, temp = _del_none_keys(result_)
    print("missing_ids", len(temp))
    print(temp)
    result.update(result_)
    return result


def _get_dynamic_meshes(cg, node_ids: Sequence[np.uint64]) -> Tuple[Dict, List]:
    result = {}
    missing_ids = []
    if not len(node_ids):
        return result, missing_ids
    mesh_dir = cg.meta.custom_data.get("mesh", {}).get("dir", "graphene_meshes")
    mesh_path = f"{cg.meta.data_source.WATERSHED}/{mesh_dir}/dynamic"

    cf = CloudFiles(mesh_path)
    filenames = [get_mesh_name(cg, id_) for id_ in node_ids]
    existence_dict = cf.exists(filenames)

    for mesh_key in existence_dict:
        node_id = np.uint64(mesh_key.split(":")[0])
        if existence_dict[mesh_key]:
            result[node_id] = mesh_key
            continue
        missing_ids.append(node_id)
    missing_ids = np.array(missing_ids, dtype=NODE_ID)
    return result, missing_ids


def _get_initial_and_dynamic_meshes(
    cg,
    shard_readers: Dict,
    node_ids: Sequence[np.uint64],
) -> Tuple[Dict, Dict, List]:
    if not len(node_ids):
        return {}, {}, []

    node_ids = np.array(node_ids, dtype=NODE_ID)
    initial_ids, new_ids = segregate_node_ids(cg, node_ids)
    print("new_ids, initial_ids", new_ids.size, initial_ids.size)
    initial_meshes_d = _get_initial_meshes(cg, shard_readers, initial_ids)
    new_meshes_d, missing_ids = _get_dynamic_meshes(cg, new_ids)
    return initial_meshes_d, new_meshes_d, missing_ids


def check_skips(cg, node_ids: Sequence[np.uint64], children_cache: dict = {}):
    """
    If a node ID has a single child, it is considered a skip.
    Such IDs won't have meshes because the child mesh will be identical.
    """
    start = time()
    layers = cg.get_chunk_layers(node_ids)
    skips = []
    result = [empty_1d, node_ids[layers == 2]]
    children_d = cg.get_children(node_ids[layers > 2])
    for p, c in children_d.items():
        if c.size > 1:
            result.append([p])
            children_cache[p] = c
            continue
        assert c.size == 1, f"{p} does not seem to have children."
        skips.append(c[0])
    print(f"skips {len(skips)}, total {len(node_ids)}, time {time()-start}")
    return np.concatenate(result), np.array(skips, dtype=np.uint64)


def segregate_node_ids(cg, node_ids):
    """
    Group node IDs based on timestamp
    initial = created at the time of ingest
    new = created by proofreading edit operations
    """
    from datetime import datetime

    initial_ts = cg.meta.custom_data["mesh"]["initial_ts"]
    initial_mesh_dt = np.datetime64(datetime.fromtimestamp(initial_ts))
    node_ids_ts = cg.get_node_timestamps(node_ids)
    initial_mesh_mask = node_ids_ts < initial_mesh_dt
    initial_ids = node_ids[initial_mesh_mask]
    new_ids = node_ids[~initial_mesh_mask]
    return initial_ids, new_ids


def get_mesh_paths(
    cg,
    node_ids: Sequence[np.uint64],
    stop_layer: int = 2,
) -> Dict:
    shard_readers = CloudVolume(  # pylint: disable=no-member
        f"graphene://https://localhost/segmentation/table/dummy",
        mesh_dir=cg.meta.custom_data.get("mesh", {}).get("dir", "graphene_meshes"),
        info=get_json_info(cg),
    ).mesh

    result = {}
    node_layers = cg.get_chunk_layers(node_ids)
    while np.any(node_layers > stop_layer):
        node_ids = node_ids[node_layers > 1]
        resp = _get_initial_and_dynamic_meshes(cg, shard_readers, node_ids)
        initial_meshes_d, new_meshes_d, missing_ids = resp
        result.update(initial_meshes_d)
        result.update(new_meshes_d)
        node_ids = cg.get_children(missing_ids, flatten=True)
        node_layers = cg.get_chunk_layers(node_ids)

    # check for left over level 2 IDs
    node_ids = node_ids[node_layers > 1]
    print("node_ids left over", node_ids.size)
    resp = _get_initial_and_dynamic_meshes(cg, shard_readers, node_ids)
    initial_meshes_d, new_meshes_d, _ = resp
    result.update(initial_meshes_d)
    result.update(new_meshes_d)
    return result


def get_children_before_start_layer(
    cg: ChunkedGraph, node_id: np.uint64, start_layer: int, bounding_box=None
):
    if cg.get_chunk_layer(node_id) == 2:
        return np.array([node_id], dtype=NODE_ID)
    result = [empty_1d]
    parents = np.array([node_id], dtype=np.uint64)
    while parents.size:
        children = cg.get_children(parents, flatten=True)
        bound_mask = misc_utils.mask_nodes_by_bounding_box(
            cg.meta, children, bounding_box=bounding_box
        )
        layers = cg.get_chunk_layers(children)
        result.append(children[(layers <= start_layer) & bound_mask])
        parents = children[(layers > start_layer) & bound_mask]
    return np.concatenate(result)
