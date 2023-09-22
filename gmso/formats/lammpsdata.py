"""Read and write LAMMPS data files."""
from __future__ import division

import copy
import datetime
import os
import re
import warnings
from pathlib import Path

import numpy as np
import unyt as u
from sympy import Symbol
from unyt.array import allclose_units

import gmso
from gmso.abc.abstract_site import MoleculeType
from gmso.core.angle import Angle
from gmso.core.angle_type import AngleType
from gmso.core.atom import Atom
from gmso.core.atom_type import AtomType
from gmso.core.bond import Bond
from gmso.core.bond_type import BondType
from gmso.core.box import Box
from gmso.core.dihedral import Dihedral
from gmso.core.element import element_by_mass
from gmso.core.improper import Improper
from gmso.core.topology import Topology
from gmso.core.views import PotentialFilters

pfilter = PotentialFilters.UNIQUE_SORTED_NAMES
from gmso.exceptions import NotYetImplementedWarning
from gmso.formats.formats_registry import loads_as, saves_as
from gmso.lib.potential_templates import PotentialTemplateLibrary
from gmso.utils.compatibility import check_compatibility
from gmso.utils.conversions import (
    convert_opls_to_ryckaert,
    convert_ryckaert_to_opls,
)
from gmso.utils.sorting import sort_by_types
from gmso.utils.units import LAMMPS_UnitSystems, write_out_parameter_and_units


@saves_as(".lammps", ".lammpsdata", ".data")
def write_lammpsdata(
    top,
    filename,
    atom_style="full",
    unit_style="real",
    strict_potentials=False,
    strict_units=False,
    lj_cfactorsDict=None,
):
    """Output a LAMMPS data file.

    Outputs a LAMMPS data file in the 'full' atom style format.
    Assumes use of 'real' units.
    See http://lammps.sandia.gov/doc/atom_style.html for more information on atom styles.

    Parameters
    ----------
    Topology : `Topology`
        A Topology Object
    filename : str
        Path of the output file
    atom_style : str, optional, default='full'
        Defines the style of atoms to be saved in a LAMMPS data file.
        The following atom styles are currently supported: 'full', 'atomic', 'charge', 'molecular'
        see http://lammps.sandia.gov/doc/atom_style.html for more information on atom styles.
    unit_style : str, optional, default='real'
        Can be any of "real", "lj", "metal", "si", "cgs", "electron", "micro", "nano". Otherwise
        an error will be thrown. These are defined in _unit_style_factory. See
        https://docs.lammps.org/units.html for LAMMPS documentation.
    strict_potentials : bool, optional, default False
        Tells the writer how to treat conversions. If False, then check for conversions
        to usable potential styles found in default_parameterMaps. If True, then error if
        potentials are not compatible.
    strict_units : bool, optional, default False
        Tells the writer how to treat unit conversions. If False, then check for conversions
        to unit styles defined in _unit_style_factory. If True, then error if parameter units
        do not match.
    lj_cfactorsDict : (None, dict), optional, default None
        If using unit_style="lj" only, can pass a dictionary with keys of ("mass", "energy",
        "length", "charge"), or any combination of these, and they will be used to non-
        dimensionalize all values in the topology. If any key is not passed, default values
        will be pulled from the topology (see _default_lj_val). These are the largest: sigma,
        epsilon, atomtype.mass, and atomtype.charge from the topology.

    Notes
    -----
    See http://lammps.sandia.gov/doc/2001/data_format.html for a full description of the LAMMPS data format.
    This is a work in progress, as only a subset of everything LAMMPS supports is currently available.
    However, please raise issues as the current writer has been set up to eventually grow to support
    all LAMMPS styles.

    Some of this function has been adopted from `mdtraj`'s support of the LAMMPSTRJ trajectory format.
    See https://github.com/mdtraj/mdtraj/blob/master/mdtraj/formats/lammpstrj.py for details.

    """
    if atom_style not in ["full", "atomic", "molecular", "charge"]:
        raise ValueError(
            'Atom style "{}" is invalid or is not currently supported'.format(
                atom_style
            )
        )

    if unit_style not in [
        "real",
        "lj",
        "metal",
        "si",
        "cgs",
        "electron",
        "micro",
        "nano",
    ]:
        raise ValueError(
            'Unit style "{}" is invalid or is not currently supported'.format(
                unit_style
            )
        )
    if unit_style != "lj" and lj_cfactorsDict:
        raise ValueError(
            "lj_cfactorsDict argument is only used if unit_style is lj."
        )
    base_unyts = LAMMPS_UnitSystems(unit_style)
    default_parameterMaps = {  # TODO: sites are not checked currently because gmso
        # doesn't store pair potential eqn the same way as the connections.
        "impropers": "HarmonicImproperPotential",
        "dihedrals": "OPLSTorsionPotential",
        "angles": "LAMMPSHarmonicAnglePotential",
        "bonds": "LAMMPSHarmonicBondPotential",
        # "sites":"LennardJonesPotential",
        # "sites":"CoulombicPotential"
    }

    # TODO: Use strict_x, (i.e. x=bonds) to validate what topology attrs to convert

    if strict_potentials:
        _validate_potential_compatibility(top)
    else:
        _try_default_potential_conversions(top, default_parameterMaps)

    if strict_units:
        _validate_unit_compatibility(top, base_unyts)
    else:
        if base_unyts and unit_style != "lj":
            lj_cfactorsDict = None
        else:  # LJ unit styles
            if lj_cfactorsDict is None:
                lj_cfactorsDict = {}
            source_factorsList = list(lj_cfactorsDict.keys())
            defaultsList = ["length", "energy", "mass", "charge"]
            for source_factor in defaultsList + source_factorsList:
                if source_factor not in defaultsList:
                    raise ValueError(
                        f"Conversion factor {source_factor} is not used. Pleas only provide some of {defaultsList}"
                    )
                if lj_cfactorsDict.get(source_factor):
                    continue
                default_val_from_topology = _default_lj_val(top, source_factor)
                lj_cfactorsDict[source_factor] = lj_cfactorsDict.get(
                    source_factor, default_val_from_topology
                )

    path = Path(filename)
    if not path.parent.exists():
        msg = "Provided path to file that does not exist"
        raise FileNotFoundError(msg)

    with open(path, "w") as out_file:
        _write_header(out_file, top, atom_style)
        _write_box(out_file, top, base_unyts, lj_cfactorsDict)
        if top.is_fully_typed():
            _write_atomtypes(out_file, top, base_unyts, lj_cfactorsDict)
            _write_pairtypes(out_file, top, base_unyts, lj_cfactorsDict)
            if top.bond_types:
                _write_bondtypes(out_file, top, base_unyts, lj_cfactorsDict)
            if top.angle_types:
                _write_angletypes(out_file, top, base_unyts, lj_cfactorsDict)
            if top.dihedral_types:
                _write_dihedraltypes(out_file, top, base_unyts, lj_cfactorsDict)
            if top.improper_types:
                _write_impropertypes(out_file, top, base_unyts, lj_cfactorsDict)

        _write_site_data(out_file, top, atom_style, base_unyts, lj_cfactorsDict)
        for conn in ["bonds", "angles", "dihedrals", "impropers"]:
            connIter = getattr(top, conn)
            if connIter:
                _write_conn_data(out_file, top, connIter, conn)


@loads_as(".lammps", ".lammpsdata", ".data")
def read_lammpsdata(
    filename,
    atom_style="full",
    unit_style="real",
):
    """Read in a lammps data file as a GMSO topology.

    Parameters
    ----------
    filename : str
        LAMMPS data file
    atom_style : str, optional, default='full'
        Inferred atom style defined by LAMMPS, be certain that this is provided
        accurately.
    unit_style : str, optional, default='real
        LAMMPS unit style used for writing the datafile. Can be "real", "lj",
        "metal", "si", "cgs", "electron", "micro", "nano".

    Returns
    -------
    top : GMSO Topology
        A GMSO Topology object

    Notes
    -----
    See http://lammps.sandia.gov/doc/2001/data_format.html for a full description of the LAMMPS data format.

    This is a work in progress, as only several atom styles, potential styles, and unit styes are currently supported.

    Currently supporting the following atom styles: 'full'

    Currently supporting the following unit styles: 'real', "real", "lj", "metal", "si", "cgs",
    "electron", "micro", "nano".

    Currently supporting the following potential styles: 'lj'
    Currently supporting the following bond styles: 'harmonic'
    Currently supporting the following angle styles: 'harmonic'
    Currently supporting the following dihedral styles: 'opls'
    Currently supporting the following improper styles: 'harmonic'

    """
    top = Topology()

    # Validate 'atom_style'
    if atom_style not in ["full"]:
        raise ValueError(
            'Atom Style "{}" is invalid or is not currently supported'.format(
                atom_style
            )
        )

    # Validate 'unit_style'
    if unit_style not in [
        "real",
        "lj",
        "metal",
        "si",
        "cgs",
        "electron",
        "micro",
        "nano",
    ]:
        raise ValueError(
            'Unit Style "{}" is invalid or is not currently supported'.format(
                unit_style
            )
        )
    base_unyts = LAMMPS_UnitSystems(unit_style)

    # Parse box information
    _get_box_coordinates(filename, base_unyts, top)
    # Parse atom type information
    top, type_list = _get_ff_information(filename, base_unyts, top)
    # Parse atom information
    _get_atoms(filename, top, base_unyts, type_list)
    # Parse connection (bonds, angles, dihedrals, impropers) information
    # TODO: Add more atom styles
    if atom_style in ["full"]:
        _get_connection(filename, top, base_unyts, connection_type="bond")
        _get_connection(filename, top, base_unyts, connection_type="angle")
        _get_connection(filename, top, base_unyts, connection_type="dihedral")
        _get_connection(filename, top, base_unyts, connection_type="improper")

    top.update_topology()

    return top


def get_units(base_unyts, dimension):
    """Get u.Unit for specific LAMMPS unit style with given dimension."""
    # Need separate angle units for harmonic force constant and angle
    if base_unyts.usystem.name == "lj":
        if dimension == "angle":
            return u.radian
        return u.dimensionless

    if dimension == "angle_eq":
        return (
            u.degree
        )  # LAMMPS specifies different units for some angles, such as equilibrium angles

    return u.Unit(base_unyts.usystem[dimension], registry=base_unyts.reg)


def _get_connection(filename, topology, base_unyts, connection_type):
    """Parse connection types."""
    # TODO: check for other connection types besides the defaults
    with open(filename, "r") as lammps_file:
        types = False
        for i, line in enumerate(lammps_file):
            if connection_type in line.split():
                n_connection_types = int(line.split()[0])
                types = True
            if connection_type.capitalize() in line.split():
                break
    if types == False:
        return topology
    templates = PotentialTemplateLibrary()
    connection_type_lines = open(filename, "r").readlines()[
        i + 2 : i + n_connection_types + 2
    ]
    connection_type_list = list()
    for line in connection_type_lines:
        if connection_type == "bond":
            template_potential = templates["LAMMPSHarmonicBondPotential"]
            # Multiply 'k' by 2 since LAMMPS includes 1/2 in the term
            conn_params = {
                "k": float(line.split()[1])
                * get_units(base_unyts, "energy")
                / get_units(base_unyts, "length") ** 2
                * 2,
                "r_eq": float(line.split()[2])
                * get_units(base_unyts, "length"),
            }
            name = template_potential.name
            expression = template_potential.expression
            variables = template_potential.independent_variables
            c_type = getattr(gmso, "BondType")(
                name=name,
                parameters=conn_params,
                expression=expression,
                independent_variables=variables,
            )
        elif connection_type == "angle":
            template_potential = templates["LAMMPSHarmonicAnglePotential"]
            # Multiply 'k' by 2 since LAMMPS includes 1/2 in the term
            conn_params = {
                "k": float(line.split()[1])
                * get_units(base_unyts, "energy")
                / get_units(base_unyts, "angle") ** 2
                * 2,
                "theta_eq": float(line.split()[2])
                * get_units(base_unyts, "angle_eq"),
            }
            name = template_potential.name
            expression = template_potential.expression
            variables = template_potential.independent_variables
            c_type = getattr(gmso, "AngleType")(
                name=name,
                parameters=conn_params,
                expression=expression,
                independent_variables=variables,
            )
        elif connection_type == "dihedral":
            template_potential = templates["OPLSTorsionPotential"]
            conn_params = {
                "k1": float(line.split()[1]) * get_units(base_unyts, "energy"),
                "k2": float(line.split()[2]) * get_units(base_unyts, "energy"),
                "k3": float(line.split()[3]) * get_units(base_unyts, "energy"),
                "k4": float(line.split()[4]) * get_units(base_unyts, "energy"),
            }
            name = template_potential.name
            expression = template_potential.expression
            variables = template_potential.independent_variables
            c_type = getattr(gmso, "DihedralType")(
                name=name,
                parameters=conn_params,
                expression=expression,
                independent_variables=variables,
            )
        elif connection_type == "improper":
            template_potential = templates["HarmonicImproperPotential"]
            conn_params = {
                "k": float(line.split()[2])
                * get_units(base_unyts, "energy")
                / get_units(base_unyts, "energy") ** 2
                * 2,
                "phi_eq": float(line.split()[3])
                * get_units(base_unyts, "angle_eq"),
            }
            name = template_potential.name
            expression = template_potential.expression
            variables = template_potential.independent_variables
            c_type = getattr(gmso, "ImproperType")(
                name=name,
                parameters=conn_params,
                expression=expression,
                independent_variables=variables,
            )

        connection_type_list.append(c_type)

    with open(filename, "r") as lammps_file:
        for i, line in enumerate(lammps_file):
            if connection_type + "s" in line.split():
                n_connections = int(line.split()[0])
            if connection_type.capitalize() + "s" in line.split():
                break
    connection_lines = open(filename, "r").readlines()[
        i + 2 : i + n_connections + 2
    ]
    # Determine number of sites to generate
    if connection_type == "bond":
        n_sites = 2
    elif connection_type == "angle":
        n_sites = 3
    else:
        n_sites = 4
    for i, line in enumerate(connection_lines):
        site_list = list()
        for j in range(n_sites):
            site = topology.sites[int(line.split()[j + 2]) - 1]
            site_list.append(site)
        ctype = copy.copy(connection_type_list[int(line.split()[1]) - 1])
        ctype.member_types = tuple(map(lambda x: x.atom_type.name, site_list))
        if connection_type == "bond":
            connection = Bond(
                connection_members=site_list,
                bond_type=ctype,
            )
        elif connection_type == "angle":
            connection = Angle(
                connection_members=site_list,
                angle_type=ctype,
            )
        elif connection_type == "dihedral":
            connection = Dihedral(
                connection_members=site_list,
                dihedral_type=ctype,
            )
        elif connection_type == "improper":
            connection = Improper(
                connection_members=site_list,
                improper_type=ctype,
            )
        topology.add_connection(connection)

    return topology


def _get_atoms(filename, topology, base_unyts, type_list):
    """Parse the atom information in the LAMMPS data file."""
    with open(filename, "r") as lammps_file:
        for i, line in enumerate(lammps_file):
            if "atoms" in line.split():
                n_atoms = int(line.split()[0])
            if "Atoms" in line.split():
                break
    atom_lines = open(filename, "r").readlines()[i + 2 : i + n_atoms + 2]
    for line in atom_lines:
        atom_line = line.split()
        atom_type = atom_line[2]
        charge = u.unyt_quantity(
            float(atom_line[3]), get_units(base_unyts, "charge")
        )
        coord = u.unyt_array(
            [float(atom_line[4]), float(atom_line[5]), float(atom_line[6])]
        ) * get_units(base_unyts, "length")
        site = Atom(
            charge=charge,
            position=coord,
            atom_type=copy.deepcopy(type_list[int(atom_type) - 1]),  # 0-index
            molecule=MoleculeType(atom_line[1], int(atom_line[1])),
        )
        element = element_by_mass(site.atom_type.mass.value)
        site.name = element.name if element else site.atom_type.name
        site.element = element
        topology.add_site(site)

    return topology


def _get_box_coordinates(filename, base_unyts, topology):
    """Parse box information."""
    with open(filename, "r") as lammps_file:
        for line in lammps_file:
            if "xlo" in line.split():
                break
        x_line = line.split()
        y_line = lammps_file.readline().split()
        z_line = lammps_file.readline().split()

        x = float(x_line[1]) - float(x_line[0])
        y = float(y_line[1]) - float(y_line[0])
        z = float(z_line[1]) - float(z_line[0])

        # Check if box is triclinic
        tilts = lammps_file.readline().split()
        if "xy" in tilts:
            xy = float(tilts[0])
            xz = float(tilts[1])
            yz = float(tilts[2])

            xhi = float(x_line[1]) - np.max([0.0, xy, xz, xy + xz])
            xlo = float(x_line[0]) - np.min([0.0, xy, xz, xy + xz])
            yhi = float(y_line[1]) - np.max([0.0, yz])
            ylo = float(y_line[0]) - np.min([0.0, yz])
            zhi = float(z_line[1])
            zlo = float(z_line[0])

            lx = xhi - xlo
            ly = yhi - ylo
            lz = zhi - zlo

            c = np.sqrt(lz**2 + xz**2 + yz**2)
            b = np.sqrt(ly**2 + xy**2)
            a = lx

            alpha = np.arccos((yz * ly + xy * xz) / (b * c))
            beta = np.arccos(xz / c)
            gamma = np.arccos(xy / b)

            # Box Information
            lengths = u.unyt_array([a, b, c], get_units(base_unyts, "length"))
            angles = u.unyt_array(
                [alpha, beta, gamma], get_units(base_unyts, "angle")
            )
            topology.box = Box(lengths, angles)
        else:
            # Box Information
            lengths = u.unyt_array([x, y, z], get_units(base_unyts, "length"))
            topology.box = Box(lengths)

        return topology


def _get_ff_information(filename, base_unyts, topology):
    """Parse atom-type information."""
    with open(filename, "r") as lammps_file:
        types = False
        for i, line in enumerate(lammps_file):
            if "atom" in line:
                n_atomtypes = int(line.split()[0])
                types = True
            elif "Masses" in line:
                break
    if types == False:
        return topology
    mass_lines = open(filename, "r").readlines()[i + 2 : i + n_atomtypes + 2]
    type_list = list()
    for line in mass_lines:
        atom_type = AtomType(
            name=line.split()[0],
            mass=float(line.split()[1]) * get_units(base_unyts, "mass"),
        )
        type_list.append(atom_type)

    with open(filename, "r") as lammps_file:
        for i, line in enumerate(lammps_file):
            if "Pair" in line:
                break
    # Need to figure out if we're going have mixing rules printed out
    # Currently only reading in LJ params
    warn_ljcutBool = False
    pair_lines = open(filename, "r").readlines()[i + 2 : i + n_atomtypes + 2]
    for i, pair in enumerate(pair_lines):
        if len(pair.split()) == 3:
            type_list[i].parameters["sigma"] = float(
                pair.split()[2]
            ) * get_units(base_unyts, "length")
            type_list[i].parameters["epsilon"] = float(
                pair.split()[1]
            ) * get_units(base_unyts, "energy")
        elif len(pair.split()) == 4:
            warn_ljcutBool = True

    if warn_ljcutBool:
        warnings.warn(
            "Currently not reading in LJ cutoff values."
            "These should be specified in the engine run files."
        )

    return topology, type_list


def _accepted_potentials():
    """List of accepted potentials that LAMMPS can support."""
    templates = PotentialTemplateLibrary()
    lennard_jones_potential = templates["LennardJonesPotential"]
    harmonic_bond_potential = templates["LAMMPSHarmonicBondPotential"]
    harmonic_angle_potential = templates["LAMMPSHarmonicAnglePotential"]
    periodic_torsion_potential = templates["PeriodicTorsionPotential"]
    harmonic_improper_potential = templates["HarmonicImproperPotential"]
    opls_torsion_potential = templates["OPLSTorsionPotential"]
    accepted_potentialsList = [
        lennard_jones_potential,
        harmonic_bond_potential,
        harmonic_angle_potential,
        periodic_torsion_potential,
        harmonic_improper_potential,
        opls_torsion_potential,
    ]
    return accepted_potentialsList


def _validate_potential_compatibility(top):
    """Check compatability of topology object potentials with LAMMPSDATA format."""
    pot_types = check_compatibility(top, _accepted_potentials())
    return pot_types


def _validate_unit_compatibility(top, base_unyts):
    """Check compatability of topology object units with LAMMPSDATA format."""
    for attribute in ["sites", "bonds", "angles", "dihedrals", "impropers"]:
        if attribute == "sites":
            atype = "atom_types"
        else:
            atype = attribute[:-1] + "_types"
        parametersList = [
            (parameter, name)
            for attr_type in getattr(top, atype)
            for name, parameter in attr_type.parameters.items()
        ]
        for parameter, name in parametersList:
            assert np.isclose(
                float(
                    base_unyts.convert_parameter(
                        parameter, n_decimals=6, name=name
                    )
                ),
                parameter.value,
                atol=1e-3,
            ), f"Units System {base_unyts.usystem} is not compatible with {atype} with value {parameter}"


def _write_header(out_file, top, atom_style):
    """Write Lammps file header."""
    out_file.write(
        "{} written by {} at {} using the GMSO LAMMPS Writer\n\n".format(
            os.environ.get("USER"),
            top.name if top.name is not None else "",
            str(datetime.datetime.now()),
        )
    )
    out_file.write("{:d} atoms\n".format(top.n_sites))
    if atom_style in ["full", "molecular"]:
        out_file.write("{:d} bonds\n".format(top.n_bonds))
        out_file.write("{:d} angles\n".format(top.n_angles))
        out_file.write("{:d} dihedrals\n".format(top.n_dihedrals))
        out_file.write("{:d} impropers\n\n".format(top.n_impropers))

    # TODO: allow users to specify filter_by syntax
    out_file.write(
        "{:d} atom types\n".format(len(top.atom_types(filter_by=pfilter)))
    )
    if top.n_bonds > 0 and atom_style in ["full", "molecular"]:
        out_file.write(
            "{:d} bond types\n".format(len(top.bond_types(filter_by=pfilter)))
        )
    if top.n_angles > 0 and atom_style in ["full", "molecular"]:
        out_file.write(
            "{:d} angle types\n".format(len(top.angle_types(filter_by=pfilter)))
        )
    if top.n_dihedrals > 0 and atom_style in ["full", "molecular"]:
        out_file.write(
            "{:d} dihedral types\n".format(
                len(top.dihedral_types(filter_by=pfilter))
            )
        )
    if top.n_impropers > 0 and atom_style in ["full", "molecular"]:
        out_file.write(
            "{:d} improper types\n".format(
                len(top.improper_types(filter_by=pfilter))
            )
        )

    out_file.write("\n")


def _write_box(out_file, top, base_unyts, cfactorsDict):
    """Write GMSO Topology box to LAMMPS file."""
    if allclose_units(
        top.box.angles,
        u.unyt_array([90, 90, 90], "degree"),
        rtol=1e-5,
        atol=1e-8,
    ):
        box_lengths = [
            float(
                base_unyts.convert_parameter(top.box.lengths[i], cfactorsDict)
            )
            for i in range(3)
        ]
        for i, dim in enumerate(["x", "y", "z"]):
            out_file.write(
                "{0:.6f} {1:.6f} {2}lo {2}hi\n".format(0, box_lengths[i], dim)
            )
        out_file.write("0.000000 0.000000 0.000000 xy xz yz\n")
    else:
        box_lengths = [
            float(
                base_unyts.convert_parameter(top.box.lengths[i], cfactorsDict)
            )
            for i in range(3)
        ]
        vectors = (box_lengths * top.box.get_unit_vectors().T).T

        xhi = vectors[0][0]
        yhi = vectors[1][1]
        zhi = vectors[2][2]
        xy = vectors[1][0]
        xz = vectors[2][0]
        yz = vectors[2][1]
        xlo = u.unyt_array(0, xy.units)
        ylo = u.unyt_array(0, xy.units)
        zlo = u.unyt_array(0, xy.units)

        xlo_bound = xlo + u.unyt_array(np.min([0.0, xy, xz, xy + xz]), xy.units)
        xhi_bound = xhi + u.unyt_array(np.max([0.0, xy, xz, xy + xz]), xy.units)
        ylo_bound = ylo + u.unyt_array(np.min([0.0, yz]), xy.units)
        yhi_bound = yhi + u.unyt_array(np.max([0.0, yz]), xy.units)
        zlo_bound = zlo
        zhi_bound = zhi

        out_file.write(
            "{0:.6f} {1:.6f} xlo xhi\n".format(xlo_bound.value, xhi_bound.value)
        )
        out_file.write(
            "{0:.6f} {1:.6f} ylo yhi\n".format(ylo_bound.value, yhi_bound.value)
        )
        out_file.write(
            "{0:.6f} {1:.6f} zlo zhi\n".format(zlo_bound.value, zhi_bound.value)
        )
        out_file.write(
            "{0:.6f} {1:.6f} {2:.6f} xy xz yz\n".format(
                xy.value, xz.value, yz.value
            )
        )


def _write_atomtypes(out_file, top, base_unyts, cfactorsDict):
    """Write out atomtypes in GMSO topology to LAMMPS file."""
    out_file.write("\nMasses\n")
    out_file.write(f"#\tmass ({base_unyts.usystem['mass']})\n")
    atypesView = sorted(top.atom_types(filter_by=pfilter), key=lambda x: x.name)
    for atom_type in atypesView:
        out_file.write(
            "{:d}\t{}\t# {}\n".format(
                atypesView.index(atom_type) + 1,
                base_unyts.convert_parameter(atom_type.mass, cfactorsDict),
                atom_type.name,
            )
        )


def _write_pairtypes(out_file, top, base_unyts, cfactorsDict):
    """Write out pair interaction to LAMMPS file."""
    # TODO: Handling of modified cross-interactions is not considered from top.pairpotential_types
    # Pair coefficients
    test_atomtype = top.sites[0].atom_type
    out_file.write(f"\nPair Coeffs # {test_atomtype.expression}\n")
    nb_style_orderTuple = (
        "epsilon",
        "sigma",
    )  # this will vary with new pair styles
    param_labels = [
        write_out_parameter_and_units(
            key, test_atomtype.parameters[key], base_unyts
        )
        for key in nb_style_orderTuple
    ]
    out_file.write("#\t" + "\t".join(param_labels) + "\n")
    sorted_atomtypes = sorted(
        top.atom_types(filter_by=pfilter), key=lambda x: x.name
    )
    for idx, param in enumerate(sorted_atomtypes):
        out_file.write(
            "{}\t{:7}\t\t{:7}\t\t# {}\n".format(
                idx + 1,
                *[
                    base_unyts.convert_parameter(
                        param.parameters[key], cfactorsDict, n_decimals=5
                    )
                    for key in nb_style_orderTuple
                ],
                param.name,
            )
        )


def _write_bondtypes(out_file, top, base_unyts, cfactorsDict):
    """Write out bonds to LAMMPS file."""
    # TODO: Use any accepted lammps styles (only takes harmonic now)
    test_bondtype = top.bonds[0].bond_type
    out_file.write(f"\nBond Coeffs #{test_bondtype.name}\n")
    bond_style_orderTuple = ("k", "r_eq")
    param_labels = [
        write_out_parameter_and_units(
            key, test_bondtype.parameters[key], base_unyts
        )
        for key in bond_style_orderTuple
    ]

    out_file.write("#\t" + "\t".join(param_labels) + "\n")
    bond_types = list(top.bond_types(filter_by=pfilter))
    bond_types.sort(key=lambda x: sorted(x.member_types))
    for idx, bond_type in enumerate(bond_types):
        member_types = sorted(
            [bond_type.member_types[0], bond_type.member_types[1]]
        )
        out_file.write(
            "{}\t{:7}\t{:7}\t\t# {}\t{}\n".format(
                idx + 1,
                *[
                    base_unyts.convert_parameter(
                        bond_type.parameters[key], cfactorsDict, n_decimals=6
                    )
                    for key in bond_style_orderTuple
                ],
                *member_types,
            )
        )


def _write_angletypes(out_file, top, base_unyts, cfactorsDict):
    """Write out angles to LAMMPS file."""
    # TODO: Use any accepted lammps parameters, only harmonic now
    test_angletype = top.angles[0].angle_type
    out_file.write(f"\nAngle Coeffs #{test_angletype.name}\n")
    angle_style_orderTuple = (
        "k",
        "theta_eq",
    )  # this will vary with new angle styles
    param_labels = [
        write_out_parameter_and_units(
            key, test_angletype.parameters[key], base_unyts
        )
        for key in angle_style_orderTuple
    ]
    out_file.write("#\t" + "\t".join(param_labels) + "\n")
    indexList = list(top.angle_types(filter_by=pfilter))
    indexList.sort(
        key=lambda x: (
            x.member_types[1],
            min(x.member_types[::2]),
            max(x.member_types[::2]),
        )
    )
    for idx, angle_type in enumerate(indexList):
        out_file.write(
            "{}\t{:7}\t{:7}\t#{:11s}\t{:11s}\t{:11s}\n".format(
                idx + 1,
                *[
                    base_unyts.convert_parameter(
                        angle_type.parameters[key],
                        cfactorsDict,
                        n_decimals=6,
                        name=key,
                    )
                    for key in angle_style_orderTuple
                ],
                *angle_type.member_types,
            )
        )


def _write_dihedraltypes(out_file, top, base_unyts, cfactorsDict):
    """Write out dihedrals to LAMMPS file."""
    test_dihedraltype = top.dihedrals[0].dihedral_type
    out_file.write(f"\nDihedral Coeffs #{test_dihedraltype.name}\n")
    dihedral_style_orderTuple = (
        "k1",
        "k2",
        "k3",
        "k4",
    )  # this will vary with new dihedral styles
    param_labels = [
        write_out_parameter_and_units(
            key, test_dihedraltype.parameters[key], base_unyts
        )
        for key in dihedral_style_orderTuple
    ]
    out_file.write("#\t" + "\t".join(param_labels) + "\n")
    indexList = list(top.dihedral_types(filter_by=pfilter))
    index_membersList = [
        (dihedral_type, sort_by_types(dihedral_type))
        for dihedral_type in indexList
    ]
    index_membersList.sort(key=lambda x: ([x[1][i] for i in [1, 2, 0, 3]]))
    for idx, (dihedral_type, members) in enumerate(index_membersList):
        out_file.write(
            "{}\t{:8}\t{:8}\t{:8}\t{:8}\t# {}\t{}\t{}\t{}\n".format(
                idx + 1,
                *[
                    base_unyts.convert_parameter(
                        dihedral_type.parameters[parameterStr],
                        cfactorsDict,
                        n_decimals=6,
                        name=parameterStr,
                    )
                    for parameterStr in dihedral_style_orderTuple
                ],
                *members,
            )
        )


def _write_impropertypes(out_file, top, base_unyts, cfactorsDict):
    """Write out impropers to LAMMPS file."""
    # TODO: Use any accepted lammps parameters, only harmonic now
    test_impropertype = top.impropers[0].improper_type
    out_file.write(f"\nImproper Coeffs #{test_impropertype.name}\n")
    improper_style_orderTuple = (
        "k",
        "phi_eq",
    )  # this will vary with new improper styles
    param_labels = [
        write_out_parameter_and_units(
            key, test_impropertype.parameters[key], base_unyts
        )
        for key in improper_style_orderTuple
    ]
    out_file.write("#\t" + "\t".join(param_labels) + "\n")
    indexList = list(top.improper_types(filter_by=pfilter))
    index_membersList = [
        (improper_type, sort_by_types(improper_type))
        for improper_type in indexList
    ]
    index_membersList.sort(key=lambda x: ([x[1][i] for i in [0, 1, 2, 3]]))
    for idx, (improper_type, members) in enumerate(index_membersList):
        out_file.write(
            "{}\t{:7}\t{:7}\n".format(
                idx + 1,
                *[
                    base_unyts.convert_parameter(
                        improper_type.parameters[parameterStr],
                        cfactorsDict,
                        n_decimals=6,
                        name=parameterStr,
                    )
                    for parameterStr in improper_style_orderTuple
                ],
                *improper_type,
            )
        )


def _write_site_data(out_file, top, atom_style, base_unyts, cfactorsDict):
    """Write atomic positions and charges to LAMMPS file.."""
    out_file.write(f"\nAtoms #{atom_style}\n\n")
    if atom_style == "atomic":
        atom_line = "{index:d}\t{type_index:d}\t{x:.8}\t{y:.8}\t{z:.8}\n"
    elif atom_style == "charge":
        atom_line = (
            "{index:d}\t{type_index:d}\t{charge:.8}\t{x:.8}\t{y:.8}\t{z:.8}\n"
        )
    elif atom_style == "molecular":
        atom_line = "{index:d}\t{moleculeid:d}\t{type_index:d}\t{x:.8}\t{y:.8}\t{z:.8}\n"
    elif atom_style == "full":
        atom_line = "{index:d}\t{moleculeid:d}\t{type_index:d}\t{charge:.8}\t{x:.8}\t{y:.8}\t{z:.8}\n"

    unique_sorted_typesList = sorted(
        top.atom_types(filter_by=pfilter), key=lambda x: x.name
    )
    for i, site in enumerate(top.sites):
        out_file.write(
            atom_line.format(
                index=i + 1,
                moleculeid=site.molecule.number,
                type_index=unique_sorted_typesList.index(site.atom_type) + 1,
                charge=base_unyts.convert_parameter(
                    site.charge,
                    cfactorsDict,
                    n_decimals=6,
                ),
                x=base_unyts.convert_parameter(
                    site.position[0],
                    cfactorsDict,
                    n_decimals=6,
                ),
                y=base_unyts.convert_parameter(
                    site.position[1], cfactorsDict, n_decimals=6
                ),
                z=base_unyts.convert_parameter(
                    site.position[2], cfactorsDict, n_decimals=6
                ),
            )
        )


def _angle_order_sorter(angle_typesList):
    return [angle_typesList[i] for i in [1, 0, 2]]


def _dihedral_order_sorter(dihedral_typesList):
    return [dihedral_typesList[i] for i in [1, 2, 0, 3]]


def _improper_order_sorter(improper_typesList):
    return [improper_typesList[i] for i in [0, 1, 2, 3]]


sorting_funcDict = {
    "bonds": None,
    "angles": _angle_order_sorter,
    "dihedrals": _dihedral_order_sorter,
    "impropers": _improper_order_sorter,
}


def _write_conn_data(out_file, top, connIter, connStr):
    """Write all connections to LAMMPS datafile."""
    out_file.write(f"\n{connStr.capitalize()}\n\n")
    indexList = list(
        map(
            sort_by_types,
            getattr(top, connStr[:-1] + "_types")(filter_by=pfilter),
        )
    )
    indexList.sort(key=sorting_funcDict[connStr])

    for i, conn in enumerate(getattr(top, connStr)):
        typeStr = f"{i+1:<6d}\t{indexList.index(sort_by_types(conn.connection_type))+1:<6d}\t"
        indexStr = "\t".join(
            map(
                lambda x: str(top.sites.index(x) + 1).ljust(6),
                conn.connection_members,
            )
        )
        out_file.write(typeStr + indexStr + "\n")


def _try_default_potential_conversions(top, potentialsDict):
    """Take a topology a convert all potentials to the style in potentialDict."""
    for pot_container in potentialsDict:
        if getattr(top, pot_container[:-1] + "_types"):
            top.convert_potential_styles(
                {pot_container: potentialsDict[pot_container]}
            )
        elif getattr(top, pot_container):
            raise AttributeError(
                f"Missing parameters in {pot_container} for {top.get_untyped(pot_container)}"
            )


def _default_lj_val(top, source):
    """Generate default lj non-dimensional values from topology."""
    if source == "length":
        return copy.deepcopy(
            max(list(map(lambda x: x.parameters["sigma"], top.atom_types)))
        )
    elif source == "energy":
        return copy.deepcopy(
            max(list(map(lambda x: x.parameters["epsilon"], top.atom_types)))
        )
    elif source == "mass":
        return copy.deepcopy(max(list(map(lambda x: x.mass, top.atom_types))))
    elif source == "charge":
        return copy.deepcopy(max(list(map(lambda x: x.charge, top.atom_types))))
    else:
        raise ValueError(
            f"Provided {source} for default LJ cannot be found in the topology."
        )
