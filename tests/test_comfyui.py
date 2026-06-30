"""Tests for the ComfyUI UI->API conversion — the PrimitiveNode/Reroute resolution.

These lock the load-bearing behavior: a naive conversion would leave resolution / texture_size
at 0 (ComfyUI then rejects the prompt). We assert primitives and reroute chains resolve to their
real values, and that primitive/reroute nodes are dropped from the API graph.
"""

from __future__ import annotations

from noiseweaver.pod.comfyui import inject, ui_to_api

# /object_info schema for a fake consumer node.
OBJ_INFO = {
    "Trellis2Mesh": {
        "input": {
            "required": {
                "resolution": ["INT", {"default": 0}],
                "texture_size": ["INT", {"default": 0}],
                "image": ["IMAGE"],
                "steps": ["INT", {"default": 12}],  # not linked -> default fills
            }
        }
    },
    "Trellis2LoadImageWithTransparency": {"input": {"required": {"image": ["STRING", {}]}}},
}


def _ui_workflow():
    # node 1 = consumer; node 10 = primitive(1536)->resolution; nodes 12->11(reroute)->texture_size
    return {
        "nodes": [
            {
                "id": 1,
                "type": "Trellis2Mesh",
                "inputs": [
                    {"name": "resolution", "link": 100},
                    {"name": "texture_size", "link": 201},
                    {"name": "image", "link": None},
                ],
            },
            {"id": 10, "type": "PrimitiveNode", "widgets_values": [1536]},
            {"id": 12, "type": "PrimitiveNode", "widgets_values": [4096]},
            {"id": 11, "type": "Reroute", "inputs": [{"name": "", "link": 200}]},
            {"id": 99, "type": "Note", "widgets_values": ["ignore me"]},
            {"id": 5, "type": "Trellis2LoadImageWithTransparency", "inputs": []},
        ],
        "links": [
            [100, 10, 0, 1, 0, "INT"],  # primitive 10 -> consumer.resolution
            [200, 12, 0, 11, 0, "INT"],  # primitive 12 -> reroute 11
            [201, 11, 0, 1, 1, "INT"],  # reroute 11 -> consumer.texture_size
        ],
    }


def test_primitive_resolves_to_value_not_zero():
    graph = ui_to_api(_ui_workflow(), OBJ_INFO)
    assert graph["1"]["inputs"]["resolution"] == 1536  # NOT 0


def test_reroute_chain_resolves():
    graph = ui_to_api(_ui_workflow(), OBJ_INFO)
    assert graph["1"]["inputs"]["texture_size"] == 4096  # chased through the Reroute


def test_unlinked_required_gets_default():
    graph = ui_to_api(_ui_workflow(), OBJ_INFO)
    assert graph["1"]["inputs"]["steps"] == 12


def test_primitive_reroute_note_nodes_dropped():
    graph = ui_to_api(_ui_workflow(), OBJ_INFO)
    assert set(graph.keys()) == {"1", "5"}  # primitives, reroute, note are not emitted


def test_inject_image_and_overrides_win():
    graph = ui_to_api(_ui_workflow(), OBJ_INFO)
    inject(graph, image="cutout.png", overrides={"Trellis2Mesh": {"resolution": 1024}},
           image_node_class="Trellis2LoadImageWithTransparency")
    assert graph["5"]["inputs"]["image"] == "cutout.png"
    assert graph["1"]["inputs"]["resolution"] == 1024  # override beats the resolved primitive
