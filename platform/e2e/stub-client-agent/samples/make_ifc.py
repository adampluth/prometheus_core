# This file is part of Prometheus
# https://inl.gov/super-intelligence/prometheus/
#
# Copyright 2026 Battelle Energy Alliance, LLC
# All rights reserved, see NOTICE.txt for full restrictions
# https://github.com/idaholab/prometheus_core/blob/main/NOTICE.txt
#
# Licensed under the MIT License, please see LICENSE for details
# https://github.com/idaholab/prometheus_core/blob/main/LICENSE
#
# SPDX-License-Identifier: MIT

"""Write a tiny IFC model for the e2e viewer test: a site, a building, one
storey, four walls and a floor slab. Every name in it is the project's own,
so it's fine to publish. Usage: make_ifc.py <out.ifc>"""
import sys
import ifcopenshell
import ifcopenshell.api as api

f = api.run("project.create_file", version="IFC4")
# Owner history names only the project, never a person or another organization
person = f.createIfcPerson(FamilyName="Prometheus", GivenName="Test fixture")
org = f.createIfcOrganization(Name="Prometheus project")
api.run("owner.add_application", f, application_developer=org, version="1.0",
        application_full_name="Prometheus e2e fixtures", application_identifier="prometheus-e2e")
project = api.run("root.create_entity", f, ifc_class="IfcProject", name="E2E viewer test")
api.run("unit.assign_unit", f)
ctx = api.run("context.add_context", f, context_type="Model")
body = api.run("context.add_context", f, context_type="Model", context_identifier="Body",
               target_view="MODEL_VIEW", parent=ctx)
site = api.run("root.create_entity", f, ifc_class="IfcSite", name="Test site")
building = api.run("root.create_entity", f, ifc_class="IfcBuilding", name="Test building")
storey = api.run("root.create_entity", f, ifc_class="IfcBuildingStorey", name="Ground floor")
api.run("aggregate.assign_object", f, relating_object=project, products=[site])
api.run("aggregate.assign_object", f, relating_object=site, products=[building])
api.run("aggregate.assign_object", f, relating_object=building, products=[storey])

import numpy as np
def place(product, x, y, z=0.0, rot=0.0):
    m = np.eye(4); c, s = np.cos(rot), np.sin(rot)
    m[:3, :3] = [[c, -s, 0], [s, c, 0], [0, 0, 1]]; m[:3, 3] = [x, y, z]
    api.run("geometry.edit_object_placement", f, product=product, matrix=m)

for i, (x, y, rot) in enumerate([(0, 0, 0), (6, 0, np.pi / 2), (6, 4, np.pi), (0, 4, -np.pi / 2)]):
    wall = api.run("root.create_entity", f, ifc_class="IfcWall", name=f"Wall {i + 1}")
    length = 6.0 if i % 2 == 0 else 4.0
    rep = api.run("geometry.add_wall_representation", f, context=body, length=length, height=3.0, thickness=0.2)
    api.run("geometry.assign_representation", f, product=wall, representation=rep)
    place(wall, x, y, rot=rot)
    api.run("spatial.assign_container", f, relating_structure=storey, products=[wall])

slab = api.run("root.create_entity", f, ifc_class="IfcSlab", name="Floor slab")
rep = api.run("geometry.add_slab_representation", f, context=body, depth=0.2,
              polyline=[(0.0, 0.0), (6.0, 0.0), (6.0, 4.0), (0.0, 4.0)])
api.run("geometry.assign_representation", f, product=slab, representation=rep)
place(slab, 0, 0, z=-0.2)
api.run("spatial.assign_container", f, relating_structure=storey, products=[slab])

fn = f.header.file_name
fn.name = "e2e-model.ifc"
fn.author = ("Prometheus project",)
fn.organization = ("Prometheus project",)
fn.time_stamp = "2026-11-10T00:00:00"  # fixed, so regenerating gives the same file
f.write(sys.argv[1])
