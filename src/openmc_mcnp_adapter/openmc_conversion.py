# SPDX-FileCopyrightText: 2022 UChicago Argonne, LLC
# SPDX-License-Identifier: MIT

import argparse
from math import pi
import re
import warnings
import itertools
import numpy as np
import openmc
from openmc.data import get_thermal_name
from openmc.data.ace import get_metadata
from openmc.model.surface_composite import (
    RightCircularCylinder as RCC,
    RectangularParallelepiped as RPP
)
from openmc.model import surface_composite
from openmc_mcnp_adapter import  surfaces_comparison

from .parse import parse, _COMPLEMENT_RE, _CELL_FILL_RE

def rotate_vector(v1, v2):
    """
    https://gist.github.com/aormorningstar/3e5dda91f155d7919ef6256cb057ceee
    Compute a matrix R that rotates v1 to align with v2.
    v1 and v2 must be length-3 1d numpy arrays.
    """
    # unit vectors
    u = v1 / np.linalg.norm(v1)
    Ru = v2 / np.linalg.norm(v2)
    # dimension of the space and identity
    dim = u.size
    I = np.identity(dim)
    # the cos angle between the vectors
    c = np.dot(u, Ru)
    # a small number
    eps = 1.0e-10
    if np.abs(c - 1.0) < eps:
        # same direction
        return I
    elif np.abs(c + 1.0) < eps:
        # opposite direction
        return -I
    else:
        # the cross product matrix of a vector to rotate around
        K = np.outer(Ru, u) - np.outer(u, Ru)
        # Rodrigues' formula
        return I + K + (K @ K) / (1 + c)

def get_openmc_materials(materials, cells):
    """Get OpenMC materials from MCNP materials

    Parameters
    ----------
    materials : list
        List of MCNP material information
    cells : list
        List of MCNP cells
    Returns
    -------
    dict
        Dictionary mapping MCNP material ID to dictionary using MCNP density as key and the corresponding :class:`openmc.Material` object as value.

    """
    materials_densities = list(itertools.groupby(sorted(cells, key=lambda cell: cell['material']), lambda c: (c['material'], c['density'])))
    materials_densities = sorted(list(set([md[0] for md in materials_densities])), key=lambda x: x[0])
    openmc.Material.next_id = max([mat_id for mat_id, _ in materials_densities]) + 1


    openmc_materials = {}
    for mcnp_mat_id, density in materials_densities:
        if mcnp_mat_id == 0:
            continue
        m = materials[mcnp_mat_id]
        if 'id' not in m:
            continue
        if mcnp_mat_id in openmc_materials.keys():
            material = openmc_materials[mcnp_mat_id][list(openmc_materials[mcnp_mat_id].keys())[0]].clone()
            material.name = f'M{mcnp_mat_id} with density {density}'
        else:
            material = openmc.Material(m['id'])
            material.name = f'M{mcnp_mat_id} with density {density}'
            norm_factor = sum([abs(percent) for _, percent in m['nuclides']])
            nuclide_percent = {}
            for nuclide, percent in m['nuclides']:
                if nuclide not in nuclide_percent.keys():
                    nuclide_percent[nuclide] = percent/norm_factor
                else:
                    nuclide_percent[nuclide] += percent/norm_factor

            for nuclide, percent in nuclide_percent.items():
                if '.' in nuclide:
                    zaid, xs = nuclide.split('.')
                else:
                    zaid = nuclide
                name, element, Z, A, metastable = get_metadata(int(zaid), 'mcnp')
                if percent < 0:
                    if A > 0:
                        material.add_nuclide(name, abs(percent), 'wo')
                    else:
                        material.add_element(element, abs(percent), 'wo')
                else:
                    if A > 0:
                        material.add_nuclide(name, percent, 'ao')
                    else:
                        material.add_element(element, percent, 'ao')

            if 'sab' in m:
                for sab in m['sab']:
                    if '.' in sab:
                        name, xs = sab.split('.')
                    else:
                        name = sab
                    material.add_s_alpha_beta(get_thermal_name(name))
        if density > 0:
            material.set_density('atom/b-cm', density)
        else:
            material.set_density('g/cm3', abs(density))
        if mcnp_mat_id not in openmc_materials.keys():
            openmc_materials[mcnp_mat_id]= {f'{density}': material}
        else:
            openmc_materials[mcnp_mat_id][f'{density}'] = material            
        

    return openmc_materials


def get_openmc_surfaces(surfaces, data):
    """Get OpenMC surfaces from MCNP surfaces

    Parameters
    ----------
    surfaces : list
        List of MCNP surfaces
    data : dict
        MCNP data-block information

    Returns
    -------
    dict
        Dictionary mapping surface ID to :class:`openmc.Surface` instance

    """
    # Ensure that autogenerated IDs for surfaces don't conflict
    openmc.Surface.next_id = max(s['id'] for s in surfaces) + 1

    openmc_surfaces = {}
    for s in surfaces:
        coeffs = s['coefficients']
        if s['mnemonic'] == 'p':
            if len(coeffs) == 9:
                p1 = coeffs[:3]
                p2 = coeffs[3:6]
                p3 = coeffs[6:]
                surf = openmc.Plane.from_points(p1, p2, p3, surface_id=s['id'])

                # Helper function to flip signs on plane coefficients
                def flip_sense(surf):
                    surf.a = -surf.a
                    surf.b = -surf.b
                    surf.c = -surf.c
                    surf.d = -surf.d

                # Enforce MCNP sense requirements
                if surf.d != 0.0:
                    if surf.d < 0.0:
                        flip_sense(surf)
                elif surf.c != 0.0:
                    if surf.c < 0.0:
                        flip_sense(surf)
                elif surf.b != 0.0:
                    if surf.b < 0.0:
                        flip_sense(surf)
                elif surf.a != 0.0:
                    if surf.a < 0.0:
                        flip_sense(surf)
                else:
                    raise ValueError(f"Plane {s['id']} appears to be a line? ({coeffs})")
            else:
                A, B, C, D = coeffs
                surf = openmc.Plane(surface_id=s['id'], a=A, b=B, c=C, d=D)
        elif s['mnemonic'] == 'px':
            surf = openmc.Plane(a=1, b=0, c=0, d=coeffs[0], surface_id=s['id'])
        elif s['mnemonic'] == 'py':
            surf = openmc.Plane(a=0, b=1, c=0, d=coeffs[0], surface_id=s['id'])
        elif s['mnemonic'] == 'pz':
            surf = openmc.Plane(a=0, b=0, c=1, d=coeffs[0], surface_id=s['id'])
        elif s['mnemonic'] == 'so':
            surf = openmc.Sphere(surface_id=s['id'], r=coeffs[0])
        elif s['mnemonic'] == 's':
            x0, y0, z0, R = coeffs
            surf = openmc.Sphere(surface_id=s['id'], x0=x0, y0=y0, z0=z0, r=R)
        elif s['mnemonic'] == 'sx':
            x0, R = coeffs
            surf = openmc.Sphere(surface_id=s['id'], x0=x0, r=R)
        elif s['mnemonic'] == 'sy':
            y0, R = coeffs
            surf = openmc.Sphere(surface_id=s['id'], y0=y0, r=R)
        elif s['mnemonic'] == 'sz':
            z0, R = coeffs
            surf = openmc.Sphere(surface_id=s['id'], z0=z0, r=R)
        elif s['mnemonic'] == 'c/x':
            y0, z0, R = coeffs
            surf = openmc.XCylinder(surface_id=s['id'], y0=y0, z0=z0, r=R)
        elif s['mnemonic'] == 'c/y':
            x0, z0, R = coeffs
            surf = openmc.YCylinder(surface_id=s['id'], x0=x0, z0=z0, r=R)
        elif s['mnemonic'] == 'c/z':
            x0, y0, R = coeffs
            surf = openmc.ZCylinder(surface_id=s['id'], x0=x0, y0=y0, r=R)
        elif s['mnemonic'] == 'cx':
            surf = openmc.XCylinder(surface_id=s['id'], r=coeffs[0])
        elif s['mnemonic'] == 'cy':
            surf = openmc.YCylinder(surface_id=s['id'], r=coeffs[0])
        elif s['mnemonic'] == 'cz':
            surf = openmc.ZCylinder(surface_id=s['id'], r=coeffs[0])
        elif s['mnemonic'] in ('k/x', 'k/y', 'k/z'):
            x0, y0, z0, R2 = coeffs[:4]
            if len(coeffs) > 4:
                up = (coeffs[4] == 1)
                if s['mnemonic'] == 'k/x':
                    surf = surface_composite.XConeOneSided(x0=x0, y0=y0, z0=z0, r2=R2, up=up)
                elif s['mnemonic'] == 'k/y':
                    surf = surface_composite.YConeOneSided(x0=x0, y0=y0, z0=z0, r2=R2, up=up)
                elif s['mnemonic'] == 'k/z':
                    surf = surface_composite.ZConeOneSided(x0=x0, y0=y0, z0=z0, r2=R2, up=up)
            else:
                if s['mnemonic'] == 'k/x':
                    surf = openmc.XCone(surface_id=s['id'], x0=x0, y0=y0, z0=z0, r2=R2)
                elif s['mnemonic'] == 'k/y':
                    surf = openmc.YCone(surface_id=s['id'], x0=x0, y0=y0, z0=z0, r2=R2)
                elif s['mnemonic'] == 'k/z':
                    surf = openmc.ZCone(surface_id=s['id'], x0=x0, y0=y0, z0=z0, r2=R2)
        elif s['mnemonic'] in ('kx', 'ky', 'kz'):
            x, R2 = coeffs[:2]
            if len(coeffs) > 2:
                up = (coeffs[2] == 1)
                if s['mnemonic'] == 'kx':
                    surf = surface_composite.XConeOneSided(x0=x, r2=R2, up=up)
                elif s['mnemonic'] == 'ky':
                    surf = surface_composite.YConeOneSided(y0=x, r2=R2, up=up)
                elif s['mnemonic'] == 'kz':
                    surf = surface_composite.ZConeOneSided(z0=x, r2=R2, up=up)
            else:
                if s['mnemonic'] == 'kx':
                    surf = openmc.XCone(surface_id=s['id'], x0=x, r2=R2)
                elif s['mnemonic'] == 'ky':
                    surf = openmc.YCone(surface_id=s['id'], y0=x, r2=R2)
                elif s['mnemonic'] == 'kz':
                    surf = openmc.ZCone(surface_id=s['id'], z0=x, r2=R2)
        elif s['mnemonic'] == 'gq':
            a, b, c, d, e, f, g, h, j, k = coeffs
            surf = openmc.Quadric(surface_id=s['id'], a=a, b=b, c=c, d=d, e=e,
                                  f=f, g=g, h=h, j=j, k=k)
        elif s['mnemonic'] == 'tx':
            x0, y0, z0, a, b, c = coeffs
            surf = openmc.XTorus(surface_id=s['id'], x0=x0, y0=y0, z0=z0, a=a, b=b, c=c)
        elif s['mnemonic'] == 'ty':
            x0, y0, z0, a, b, c = coeffs
            surf = openmc.YTorus(surface_id=s['id'], x0=x0, y0=y0, z0=z0, a=a, b=b, c=c)
        elif s['mnemonic'] == 'tz':
            x0, y0, z0, a, b, c = coeffs
            surf = openmc.ZTorus(surface_id=s['id'], x0=x0, y0=y0, z0=z0, a=a, b=b, c=c)
        elif s['mnemonic'] in ('x', 'y', 'z'):
            axis = s['mnemonic'].upper()
            cls_plane = getattr(openmc, f'{axis}Plane')
            cls_cylinder = getattr(openmc, f'{axis}Cylinder')
            cls_cone = getattr(surface_composite, f'{axis}ConeOneSided')
            if len(coeffs) == 4:
                x1, r1, x2, r2 = coeffs
                if x1 == x2:
                    surf = cls_plane(x1, surface_id=s['id'])
                elif r1 == r2:
                    surf = cls_cylinder(r=r1, surface_id=s['id'])
                else:
                    dr = r2 - r1
                    dx = x2 - x1
                    grad = dx/dr
                    offset = x2 - grad*r2
                    angle = (-1/grad)**2

                    # decide if we want the up or down part of the
                    # cone since one sheet is used
                    up = grad >= 0
                    surf = cls_cone(**{f"{s['mnemonic']}0": offset, "r2": angle, "up": up})
            else:
                raise NotImplementedError(f"{s['mnemonic']} surface with {len(coeffs)} parameters")
        elif s['mnemonic'] == 'rcc':
            vx, vy, vz, hx, hy, hz, r = coeffs
            if hx == 0.0 and hy == 0.0:
                surf = RCC((vx, vy, vz), hz, r, axis='z')
            elif hy == 0.0 and hz == 0.0:
                surf = RCC((vx, vy, vz), hx, r, axis='x')
            elif hx == 0.0 and hz == 0.0:
                surf = RCC((vx, vy, vz), hy, r, axis='y')
            else:
                d = np.sqrt(hx**2 + hy**2 + hz**2)
                vz0 = np.array([0, 0, 1])
                v = np.array([hx, hy, hz])
                rotation_matrix = rotate_vector(vz0, v)
                surf = RCC((vx, vy, vz), d, r, axis='z').rotate(rotation_matrix, pivot=(vx, vy, vz))
        elif s['mnemonic'] == 'rpp':
            surf = RPP(*coeffs)
        elif s['mnemonic'] == 'box':
            if len(coeffs) == 9:
                raise NotImplementedError('BOX macrobody with one infinite dimension not supported')
            elif len(coeffs) != 12:
                raise NotImplementedError('BOX macrobody should have 12 coefficients')
            surf = surface_composite.Box(coeffs[:3], coeffs[3:6], coeffs[6:9], coeffs[9:])
        else:
            raise NotImplementedError('Surface type "{}" not supported'
                                      .format(s['mnemonic']))

        if s['reflective']:
            surf.boundary_type = 'reflective'

        if 'tr' in s:
            tr_num = s['tr']
            displacement, rotation = data['tr'][tr_num]
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", openmc.IDWarning)
                surf = surf.translate(displacement, inplace=True)
                if rotation is not None:
                    surf = surf.rotate(rotation, pivot=displacement, inplace=True) 

        openmc_surfaces[s['id']] = surf

        # For macrobodies, we also need to add generated surfaces to dictionary
        if isinstance(surf, surface_composite.CompositeSurface):
            surfaces_macrobodies = (-surf).get_surfaces()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", openmc.IDWarning)
                for i, s in surfaces_macrobodies.items():
                    if s.type == 'x-plane':
                        s = openmc.Plane(a=1, b=0, c=0, d=s.x0, surface_id=s.id)
                    if s.type == 'y-plane':
                        s = openmc.Plane(a=0, b=1, c=0, d=s.y0, surface_id=s.id) 
                    if s.type == 'z-plane':
                        s = openmc.Plane(a=0, b=0, c=1, d=s.z0, surface_id=s.id) 
                    openmc_surfaces[i] = s

    return openmc_surfaces

def get_surfaces_to_be_compared(surfaces):

    surfaces_openmc_comparison = {}
    for i, surface in surfaces.items():
        if isinstance(surface, surface_composite.CompositeSurface):
            continue
        if surface.boundary_type == 'transmission':
            surf_type = surface.type.replace('x-plane', 'plane').replace('y-plane', 'plane').replace('z-plane', 'plane')
            if surf_type in surfaces_openmc_comparison.keys():
                surfaces_openmc_comparison[surf_type] += [surface]
            else:
                surfaces_openmc_comparison[surf_type] = [surface]
    
    return surfaces_openmc_comparison

def compare_surfaces(surfaces):

    surfaces_openmc_comparison = get_surfaces_to_be_compared(surfaces)
    surfaces_planes = {}
    surfaces_others = {}
    
    for tipo, surfs in surfaces_openmc_comparison.items():
        if tipo == 'plane':
            for surf in surfs:
                surfaces_planes[surf.id] = {'id':surf.id, 'kind': 'plane', 'coefficients': list(surf.coefficients.values())}
        else:
            for surf in surfs:
                surfaces_others[surf.id] = {'id':surf.id, 'kind': tipo, 'coefficients': list(surf.coefficients.values())}
    
    
    
    identical_surfaces_planes = surfaces_comparison.compare(surfaces_planes, "Dynamic") 
    identical_surfaces_others = surfaces_comparison.compare(surfaces_others, "Dynamic") 


    identical_surfaces_temporary = identical_surfaces_planes | identical_surfaces_others

    
    identical_surfaces = {}
    for k, v in identical_surfaces_temporary.items():
        identical_surfaces[str(k)] = int((v/abs(v))*identical_surfaces.get(str(abs(v)), abs(v)))
        if int(k) in identical_surfaces.values() or int(-1*int(k)) in identical_surfaces.values():
            for k2, v2 in identical_surfaces.items():
                if abs(int(v2)) == abs(int(k)):
                    identical_surfaces[str(k2)] = int((v2/abs(v2))*v)
    return identical_surfaces

def reduce_general_plane_to_xyz(surfaces):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", openmc.IDWarning)
        new_surfaces = {}
        for k, surf in surfaces.items():
            if isinstance(surf, surface_composite.CompositeSurface):
                new_surfaces[k] = surf
            else:
                if surf.type == 'plane':
                    coeff = surf.coefficients
                    if coeff['a']==1 and coeff['b']==0 and coeff['c']==0:
                        s = openmc.XPlane(x0=coeff['d'], surface_id=surf.id, boundary_type=surf.boundary_type)
                    elif coeff['a']==0 and coeff['b']==1 and coeff['c']==0:
                        s = openmc.YPlane(y0=coeff['d'], surface_id=surf.id, boundary_type=surf.boundary_type)
                    elif coeff['a']==0 and coeff['b']==0 and coeff['c']==1:
                        s = openmc.ZPlane(z0=coeff['d'], surface_id=surf.id, boundary_type=surf.boundary_type)
                    else:
                        s = surf
                    new_surfaces[k] = s
                else:
                    new_surfaces[k] = surf
    return new_surfaces

def get_openmc_universes(cells, surfaces, materials, data, compare_remove_surfaces=True):
    """Get OpenMC surfaces from MCNP surfaces

    Parameters
    ----------
    cells : list
        List of MCNP cells
    surfaces : dict
        Dictionary mapping surface ID to :class:`openmc.Surface`
    materials : dict
        Dictionary mapping material ID to :class:`openmc.Material`
    data : dict
        MCNP data-block information

    Returns
    -------
    dict
        Dictionary mapping universe ID to :class:`openmc.Universe` instance

    """
    openmc_cells = {}
    cell_by_id = {c['id']: c for c in cells}
    universes = {}
    root_universe = openmc.Universe(0)
    universes[0] = root_universe

    # Determine maximum IDs so that autogenerated IDs don't conflict
    openmc.Cell.next_id = max(c['id'] for c in cells) + 1
    all_univ_ids = set()
    for c in cells:
        if 'u' in c['parameters']:
            all_univ_ids.add(abs(int(c['parameters']['u'])))
    if all_univ_ids:
        openmc.Universe.next_id = max(all_univ_ids) + 1

    # Cell-complements pose a unique challenge for conversion because the
    # referenced cell may have a region that was translated, so we can't simply
    # replace the cell-complement by what appears on the referenced
    # cell. Instead, we loop over all the cells and construct regions for all
    # cells without cell complements. Then, we handle the remaining cells by
    # replacing the cell-complement with the string representation of the actual
    # region that was already converted
    has_cell_complement = []
    translate_memo = {}
    for c in cells:
        # Skip cells that have cell-complements to be handled later
        match = _COMPLEMENT_RE.search(c['region'])
        if match:
            has_cell_complement.append(c)
            continue

        # Assign region to cell based on expression
        region = c['region'].replace('#', '~').replace(':', '|')
        try:
            c['_region'] = openmc.Region.from_expression(region, surfaces)
        except Exception:
            raise ValueError('Could not parse region for cell (ID={}): {}'
                             .format(c['id'], region))

        if 'trcl' in c['parameters'] or '*trcl' in c['parameters']:
            if 'trcl' in c['parameters']:
                trcl = c['parameters']['trcl'].strip()
                use_degrees = False
            else:
                trcl = c['parameters']['*trcl'].strip()
                use_degrees = True

            # Apply transformation to fill
            if 'fill' in c['parameters']:
                fill = c['parameters']['fill']
                if use_degrees:
                    c['parameters']['*fill'] = f'{fill} {trcl}'
                    c['parameters'].pop('fill')
                else:
                    c['parameters']['fill'] = f'{fill} {trcl}'

            if not trcl.startswith('('):
                raise NotImplementedError(
                    'TRn card not supported (cell {}).'.format(c['id']))

            # Drop parentheses
            trcl = trcl[1:-1].split()

            vector = tuple(float(c) for c in trcl[:3])
            c['_region'] = c['_region'].translate(vector, translate_memo)

            if len(trcl) > 3:
                rotation_matrix = np.array([float(x) for x in trcl[3:]]).reshape((3, 3))
                if use_degrees:
                    rotation_matrix = np.cos(rotation_matrix * pi/180.0)
                print(rotation_matrix)
                c['_region'] = c['_region'].rotate(rotation_matrix.T, pivot=vector)

            # Update surfaces dictionary with new surfaces
            for surf_id, surf in c['_region'].get_surfaces().items():
                surfaces[surf_id] = surf
                if isinstance(surf, surface_composite.CompositeSurface):
                    surfaces_macrobodies = (-surf).get_surfaces()
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", openmc.IDWarning)
                        for i, s in surfaces_macrobodies.items():
                            if s.type == 'x-plane':
                                s = openmc.Plane(a=1, b=0, c=0, d=s.x0, surface_id=s.id)
                            if s.type == 'y-plane':
                                s = openmc.Plane(a=0, b=1, c=0, d=s.y0, surface_id=s.id) 
                            if s.type == 'z-plane':
                                s = openmc.Plane(a=0, b=0, c=1, d=s.z0, surface_id=s.id) 
                            surfaces[i] = s

    has_cell_complement_ordered = []
    def add_to_ordered(c):
        region = c['region']
        matches = _COMPLEMENT_RE.findall(region)
        for _, other_id in matches:
            other_cell = cell_by_id[int(other_id)]
            if other_cell in has_cell_complement:
                add_to_ordered(other_cell)
        if c not in has_cell_complement_ordered:
            has_cell_complement_ordered.append(c)
    for c in has_cell_complement:
        add_to_ordered(c)

    # Now that all cells without cell-complements have been handled, we loop
    # over the remaining ones and convert any cell-complement expressions by
    # using str(region)
    for c in has_cell_complement_ordered:
        # Replace cell-complement with regular complement
        region = c['region']
        matches = _COMPLEMENT_RE.findall(region)
        assert matches
        for _, other_id in matches:
            other_cell = cell_by_id[int(other_id)]
            try:
                r = ~other_cell['_region']
            except KeyError:
                raise NotImplementedError(
                    'Cannot handle nested cell-complements for cell {}: {}'
                    .format(c['id'], c['region']))
            region = _COMPLEMENT_RE.sub(str(r), region, count=1)

        # Assign region to cell based on expression
        region = region.replace('#', '~').replace(':', '|')
        try:
            c['_region'] = openmc.Region.from_expression(region, surfaces)
        except Exception:
            raise ValueError('Could not parse region for cell (ID={}): {}'
                             .format(c['id'], region))

        # assume these cells are not translated themselves
        assert 'trcl' not in c['parameters']

    # Now that all cell regions have been converted, the next loop is to create
    # actual Cell/Universe/Lattice objects
    if compare_remove_surfaces:
        identical_surfaces = compare_surfaces(surfaces)
        #import json
        #with open('dictionary_surface_replaced.json', 'w') as fp:
        #    json.dump(identical_surfaces, fp)

    surfaces = reduce_general_plane_to_xyz(surfaces)
    
    for c in cells:
        cell = openmc.Cell(cell_id=c['id'])

        # Assign region to cell based on expression
        if compare_remove_surfaces:
            cell_surfaces = c['_region'].get_surfaces().keys()
            region_definition = ' ' + str(c['_region']).replace('(', ' ( ').replace(')', ' ) ').replace('+', ' ') + ' '
            to_be_replaced = [str(int(surf)) for surf in cell_surfaces if str(surf) in identical_surfaces.keys()]
            for surf_id in to_be_replaced:
                surf_new = int(identical_surfaces[surf_id])
                region_definition = region_definition.replace(f' -{surf_id} ', f' {int(-1*surf_new)} ').replace(f' {surf_id} ',f' {surf_new} ')
            cell.region = openmc.Region.from_expression(region_definition, surfaces)
        else:
            cell.region = openmc.Region.from_expression(str(c['_region']), surfaces)

        # Add cell to universes if necessary
        if 'u' in c['parameters']:
            if 'lat' not in c['parameters']:
                # Note: a negative universe indicates that the cell is not
                # truncated by the boundary of a higher level cell.
                uid = abs(int(c['parameters']['u']))
                if uid not in universes:
                    universes[uid] = openmc.Universe(uid)
                universes[uid].add_cell(cell)
        else:
            root_universe.add_cell(cell)

        # Look for vacuum boundary condition
        if isinstance(cell.region, openmc.Union):
            if all([isinstance(n, openmc.Halfspace) for n in cell.region]):
                if 'imp:n' in c['parameters'] and f"{float(c['parameters']['imp:n']):.5f}" == f"{0:.5f}":
                    for n in cell.region:
                        if n.surface.boundary_type == 'transmission':
                            n.surface.boundary_type = 'vacuum'
                    root_universe.remove_cell(cell)
        elif isinstance(cell.region, openmc.Halfspace):
            if 'imp:n' in c['parameters'] and f"{float(c['parameters']['imp:n']):.5f}" == f"{0:.5f}":
                if cell.region.surface.boundary_type == 'transmission':
                    cell.region.surface.boundary_type = 'vacuum'
                root_universe.remove_cell(cell)

        # Determine material fill if present -- this is not assigned until later
        # in case it's used in a lattice (need to create an extra universe then)
        if c['material'] > 0:
            mat = materials[c['material']][f'{c["density"]}']
        
        # Create lattices
        if 'fill' in c['parameters'] or '*fill' in c['parameters']:
            if 'lat' in c['parameters']:
                # Check what kind of lattice this is
                if int(c['parameters']['lat']) == 2:
                    raise NotImplementedError("Hexagonal lattices not supported")

                # Cell filled with Lattice
                uid = abs(int(c['parameters']['u']))
                if uid not in universes:
                    universes[uid] = openmc.RectLattice(uid)
                lattice = universes[uid]

                # Determine dimensions of single lattice element
                if len(cell.region) < 4:
                    raise NotImplementedError('One-dimensional lattices not supported')
                sides = {'x': [], 'y': [], 'z': []}
                for n in cell.region:
                    if isinstance(n.surface, openmc.XPlane):
                        sides['x'].append(n.surface.x0)
                    elif isinstance(n.surface, openmc.YPlane):
                        sides['y'].append(n.surface.y0)
                    elif isinstance(n.surface, openmc.ZPlane):
                        sides['z'].append(n.surface.z0)
                if not sides['x'] or not sides['y']:
                    raise NotImplementedError('2D lattice with basis other than x-y not supported')

                # MCNP's convention is that across the first surface listed is
                # the (1,0,0) element and across the second surface is the
                # (-1,0,0) element
                if sides['z']:
                    v1, v0 = np.array([sides['x'], sides['y'], sides['z']]).T
                else:
                    v1, v0 = np.array([sides['x'], sides['y']]).T

                pitch = abs(v1 - v0)

                def get_universe(uid):
                    if uid not in universes:
                        universes[uid] = openmc.Universe(uid)
                    return universes[uid]

                # Get extent of lattice
                words = c['parameters']['fill'].split()

                # If there's only a single parameter, the lattice is infinite
                inf_lattice = (len(words) == 1)

                if inf_lattice:
                    # Infinite lattice
                    xmin = xmax = ymin = ymax = zmin = zmax = 0
                    univ_ids = words
                else:
                    pairs = re.findall(r'-?\d+\s*:\s*-?\d+', c['parameters']['fill'])
                    i_colon = c['parameters']['fill'].rfind(':')
                    univ_ids = c['parameters']['fill'][i_colon + 1:].split()[1:]

                    if not pairs:
                        raise ValueError('Cant find lattice specification')

                    xmin, xmax = map(int, pairs[0].split(':'))
                    ymin, ymax = map(int, pairs[1].split(':'))
                    zmin, zmax = map(int, pairs[2].split(':'))
                    assert xmax >= xmin
                    assert ymax >= ymin
                    assert zmax >= zmin

                if pitch.size == 3:
                    index0 = np.array([xmin, ymin, zmin])
                    index1 = np.array([xmax, ymax, zmax])
                else:
                    index0 = np.array([xmin, ymin])
                    index1 = np.array([xmax, ymax])
                shape = index1 - index0 + 1

                # Determine lower-left corner of lattice
                corner0 = v0 + index0*(v1 - v0)
                corner1 = v1 + index1*(v1 - v0)
                lower_left = np.min(np.vstack((corner0, corner1)), axis=0)

                lattice.pitch = pitch
                lattice.lower_left = lower_left
                lattice.dimension = shape

                # Universe IDs array as ([z], y, x)
                univ_ids = np.asarray(univ_ids, dtype=int)
                univ_ids.shape = shape[::-1]

                # Depending on the order of the surfaces listed, it may be
                # necessary to flip some axes
                if (v1 - v0)[0] < 0.:
                    # lattice positions on x-axis are backwards
                    univ_ids = np.flip(univ_ids, axis=-1)
                if (v1 - v0)[1] < 0.:
                    # lattice positions on y-axis are backwards
                    univ_ids = np.flip(univ_ids, axis=-2)
                if sides['z'] and (v1 - v0)[2] < 0.:
                    # lattice positions on z-axis are backwards
                    univ_ids = np.flip(univ_ids, axis=-3)

                # Check for universe ID same as the ID assigned to the cell
                # itself -- since OpenMC can't handle this directly, we need
                # to create an extra cell/universe to fill in the lattice
                if np.any(univ_ids == uid):
                    c = openmc.Cell(fill=mat)
                    u = openmc.Universe(cells=[c])
                    univ_ids[univ_ids == uid] = u.id

                    # Put it in universes dictionary so that get_universe
                    # works correctly
                    universes[u.id] = u

                # If center of MCNP lattice element is not (0,0,0), we need
                # to translate the universe
                center = np.zeros(3)
                center[:v0.size] = (v0 + v1)/2
                if not np.all(center == 0.0):
                    for uid in np.unique(univ_ids):
                        # Create translated universe
                        c = openmc.Cell(fill=get_universe(uid))
                        c.translation = -center
                        u = openmc.Universe(cells=[c])
                        universes[u.id] = u

                        # Replace original universes with translated ones
                        univ_ids[univ_ids == uid] = u.id

                # Get an array of universes instead of IDs
                lat_univ = np.vectorize(get_universe)(univ_ids)

                # Fill universes in OpenMC lattice, reversing y direction
                lattice.universes = lat_univ[..., ::-1, :]

                # For infinite lattices, set the outer universe
                if inf_lattice:
                    lattice.outer = lat_univ.ravel()[0]

                cell._lattice = True
            else:
                # Cell filled with universes
                if 'fill' in c['parameters']:
                    uid, ftrans = _CELL_FILL_RE.search(c['parameters']['fill']).groups()
                    use_degrees = False
                else:
                    uid, ftrans = _CELL_FILL_RE.search(c['parameters']['*fill']).groups()
                    use_degrees = True

                # First assign fill based on whether it is a universe/lattice
                uid = int(uid)
                if uid not in universes:
                    for ci in cells:
                        if 'u' in ci['parameters']:
                            if abs(int(ci['parameters']['u'])) == uid:
                                if 'lat' in ci['parameters']:
                                    universes[uid] = openmc.RectLattice(uid)
                                else:
                                    universes[uid] = openmc.Universe(uid)
                                break
                cell.fill = universes[uid]

                # Set fill transformation
                if ftrans is not None:
                    ftrans = ftrans.split()
                    if len(ftrans) > 3:
                        cell.translation = tuple(float(x) for x in ftrans[:3])
                        rotation_matrix = np.array([float(x) for x in ftrans[3:]]).reshape((3, 3))
                        if use_degrees:
                            rotation_matrix = np.cos(rotation_matrix * pi/180.0)
                        cell.rotation = rotation_matrix
                    elif len(ftrans) < 3:
                        assert len(ftrans) == 1
                        tr_num = int(ftrans[0])
                        translation, rotation = data['tr'][tr_num]
                        cell.translation = translation
                        if rotation is not None:
                            cell.rotation = rotation.T
                    else:
                        cell.translation = tuple(float(x) for x in ftrans)

        elif c['material'] > 0:
            cell.fill = mat
        
        if 'vol' in c["parameters"]:
            cell.volume = float(c["parameters"]["vol"])

        if not hasattr(cell, '_lattice'):
            openmc_cells[c['id']] = cell

    # Expand shorthand notation
    def replace_complement(region, cells):
        if isinstance(region, (openmc.Intersection, openmc.Union)):
            for n in region:
                replace_complement(n, cells)
        elif isinstance(region, openmc.Complement):
            if isinstance(region.node, openmc.Halfspace):
                region.node = cells[region.node.surface.id].region

    for cell in openmc_cells.values():
        replace_complement(cell.region, openmc_cells)
    return universes


def mcnp_to_model(filename, compare_remove_surfaces=True):
    """Convert MCNP input to OpenMC model

    Parameters
    ----------
    filename : str
        Path to MCNP file

    Returns
    -------
    openmc.Model
        Equivalent OpenMC model

    """

    cells, surfaces, data = parse(filename)
    openmc_materials = get_openmc_materials(data['materials'], cells)
    openmc_surfaces = get_openmc_surfaces(surfaces, data)
    openmc_universes = get_openmc_universes(cells, openmc_surfaces,
                                            openmc_materials, data, compare_remove_surfaces)

    geometry = openmc.Geometry(openmc_universes[0])
    materials = openmc.Materials(geometry.get_all_materials().values())

    settings = openmc.Settings()
    settings.batches = 40
    settings.inactive = 20
    settings.particles = 100
    settings.output = {'summary': True}

    # Determine bounding box for geometry
    all_volume = openmc.Union([cell.region for cell in
                                geometry.root_universe.cells.values()])
    ll, ur = all_volume.bounding_box
    if np.any(np.isinf(ll)) or np.any(np.isinf(ur)):
        settings.source = openmc.IndependentSource(space=openmc.stats.Point())
    else:
        settings.source = openmc.IndependentSource(space=openmc.stats.Point((ll + ur)/2))

    return openmc.Model(geometry, materials, settings)


def mcnp_to_openmc():
    """Command-line interface for converting MCNP model"""
    parser = argparse.ArgumentParser()
    parser.add_argument('mcnp_filename')
    parser.add_argument("--compare_surfaces", action="store_true", 
                    help="compare and remove identical surfaces", required=False)
    
    parser.add_argument('--no-compare_surfaces', dest='compare_surfaces', action='store_false')
    parser.set_defaults(compare_surfaces=False)
    args = parser.parse_args()

    model = mcnp_to_model(args.mcnp_filename, args.compare_surfaces)
    model.export_to_xml()
