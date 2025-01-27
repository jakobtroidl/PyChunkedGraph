from time import time

import numpy as np
from cloudvolume import CloudVolume

from .utils import get_children_before_start_layer
from ...graph import ChunkedGraph
from ...graph.types import empty_1d
from ...graph.utils.basetypes import NODE_ID
from ...graph.utils import generic as misc_utils
from ...graph.chunks import utils as chunk_utils


def verified_manifest(
    cg: ChunkedGraph,
    node_id: np.uint64,
    start_layer: int,
    bounding_box=None,
):
    from .utils import get_mesh_paths

    start = time()
    bounding_box = chunk_utils.normalize_bounding_box(
        cg.meta, bounding_box, bbox_is_coordinate=True
    )
    node_ids = get_children_before_start_layer(
        cg, node_id, start_layer, bounding_box=bounding_box
    )
    print(f"children before start_layer count {len(node_ids)}, time {time() - start}")

    start = time()
    result = get_mesh_paths(cg, node_ids)
    node_ids = np.fromiter(result.keys(), dtype=NODE_ID)

    mesh_files = []
    for val in result.values():
        try:
            path, offset, size = val
            path = path.split("initial/")[-1]
            mesh_files.append(f"~{path}:{offset}:{size}")
        except:
            mesh_files.append(val)
    print(f"shard lookups took {time() - start}")
    return node_ids, mesh_files


def speculative_manifest(
    cg: ChunkedGraph,
    node_id: NODE_ID,
    start_layer: int,
    stop_layer: int = 2,
    bounding_box=None,
):
    """
    This assumes children IDs have meshes.
    Not checking for their existence reduces latency.
    """
    from .utils import check_skips
    from .utils import segregate_node_ids
    from ..meshgen_utils import get_mesh_name
    from ..meshgen_utils import get_json_info

    if start_layer is None:
        start_layer = cg.meta.custom_data.get("mesh", {}).get("max_layer", 2)

    start = time()
    bounding_box = chunk_utils.normalize_bounding_box(
        cg.meta, bounding_box, bbox_is_coordinate=True
    )
    node_ids = get_children_before_start_layer(
        cg, node_id, start_layer=start_layer, bounding_box=bounding_box
    )
    print("children_before_start_layer", time() - start)

    start = time()
    result = [empty_1d]
    node_layers = cg.get_chunk_layers(node_ids)
    while np.any(node_layers > stop_layer):
        result.append(node_ids[node_layers == stop_layer])
        ids_ = node_ids[node_layers > stop_layer]
        ids_, skips = check_skips(cg, ids_)

        result.append(ids_)
        node_ids = skips.copy()
        node_layers = cg.get_chunk_layers(node_ids)

    result.append(node_ids[node_layers == stop_layer])
    print("chilren IDs", len(result), time() - start)

    readers = CloudVolume(  # pylint: disable=no-member
        f"graphene://https://localhost/segmentation/table/dummy",
        mesh_dir=cg.meta.custom_data.get("mesh", {}).get("dir", "graphene_meshes"),
        info=get_json_info(cg),
    ).mesh.readers

    node_ids = np.concatenate(result)
    initial_ids, new_ids = segregate_node_ids(cg, node_ids)

    # get shards for initial IDs
    layers = cg.get_chunk_layers(initial_ids)
    chunk_ids = cg.get_chunk_ids_from_node_ids(initial_ids)
    mesh_shards = []
    for id_, layer, chunk_id in zip(initial_ids, layers, chunk_ids):
        fname, minishard = readers[layer].compute_shard_location(id_)
        mesh_shards.append(f"~{id_}:{layer}:{chunk_id}:{fname}:{minishard}")

    # get mesh files for new IDs
    mesh_files = [f"{get_mesh_name(cg, id_)}" for id_ in new_ids]
    return np.concatenate([initial_ids, new_ids]), mesh_shards + mesh_files
