"""Temporary generic fixtures; do not provide production taxonomies or preset model responses."""

import copy

import pytest

from confdr.shared.kubernetes import rebuild_case


@pytest.fixture
def documents():
    return [
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "app"},
            "spec": {
                "nodeSelector": {"slot": "absent"},
                "containers": [
                    {
                        "name": "main",
                        "image": "example.invalid/app:1",
                        "resources": {"requests": {"cpu": "10m", "memory": "8Mi"}},
                    }
                ],
            },
        }
    ]


@pytest.fixture
def environment():
    return {
        "WaitingDeployPods": {},
        "ClusterState": {
            "Nodes": {
                "node": {
                    "cpu": 1000,
                    "mem": 1024,
                    "remain_pods": 20,
                    "labels": {"slot": "present"},
                    "ready": True,
                    "unschedulable": False,
                    "taints": [],
                    "used_ports": [],
                }
            },
            "ExistingPods": {},
            "PVCs": {},
            "PVs": {},
            "StorageClasses": {},
        },
    }


@pytest.fixture
def case(documents, environment):
    return rebuild_case(documents, environment)


@pytest.fixture
def catalog():
    formula = {
        "quantifier": "forall",
        "variable": "p",
        "sort": "Pod",
        "body": {
            "quantifier": "forall",
            "variable": "n",
            "sort": "Node",
            "body": {
                "op": "implies",
                "args": [
                    {"call": "assign", "args": [{"var": "p"}, {"var": "n"}]},
                    {"call": "selector_ok", "args": [{"var": "p"}, {"var": "n"}]},
                ],
            },
        },
    }
    rule = {
        "id": "test.constraint/check",
        "category_id": "test.constraint",
        "title": "test constraint",
        "source_ids": ["taxonomy:test.constraint"],
        "formula": formula,
    }
    return {
        "schema_version": "2.0",
        "taxonomy": {
            "categories": [
                {
                    "id": "test.constraint",
                    "name": "Test constraint",
                    "description": "Constrain assignment by labels in tests",
                }
            ]
        },
        "knowledge": [
            {"id": "taxonomy:test.constraint", "categories": ["test.constraint"], "text": "Test source"}
        ],
        "rules": [rule],
        "repair_examples": [],
        "verification_material": {
            "origin": "external_reference",
            "reference": {"categories": {"test.constraint": [copy.deepcopy(rule)]}},
        },
    }
