import warnings
import unyt as u

from topology.utils.testing import allclose
from topology.core.potential import Potential
from topology.utils.misc import unyt_to_hashable


class AtomType(Potential):
    """An atom type, inheriting from the Potential class.

    AtomType represents an atom type and includes the functional form
    describing its interactions and, optionally, other properties such as mass
    and charge.  This class inhereits from Potential, which stores the
    non-bonded interaction between atoms or sites. The functional form of the
    potential is stored as a `sympy` expression and the parameters, with units,
    are stored explicitly.

    Parameters
    ----------
    name : str, default="AtomType"
        The name of the potential.
    mass : unyt.unyt_quantity, optional, default=0.0 * unyt.g / u.mol
        The mass of the atom type.
    charge : unyt.unyt_quantity, optional, default=0.0 * unyt.elementary_charge
        The charge of the atom type.
    expression : str or sympy.Expr,
                 default='4*epsilon*((sigma/r)**12 - (sigma/r)**6)',
        The mathematical expression describing the functional form of the
        potential describing this atom type, i.e. a Lennard-Jones potential.
        The default value is a 12-6 Lennard-Jones potential.
    parameters : dict of str : unyt.unyt_quantity pairs,
        default={'sigma': 0.3 * u.nm, 'epsilon': 0.3 * u.Unit('kJ')},
        The parameters of the potential describing this atom type and their
        values, as unyt quantities.
    independent_variables : str, sympy.Symbol, or list-like of str, sympy.Symbol
        The independent variables of the functional form previously described.

    """

    def __init__(self,
                 name='AtomType',
                 mass=0.0 * u.gram / u.mol,
                 charge=0.0 * u.elementary_charge,
                 expression='4*epsilon*((sigma/r)**12 - (sigma/r)**6)',
                 parameters={
                    'sigma': 0.3 * u.nm,
                    'epsilon': 0.3 * u.Unit('kJ')},
                 independent_variables={'r'}):

        super(AtomType, self).__init__(
            name=name,
            expression=expression,
            parameters=parameters,
            independent_variables=independent_variables)
        self._mass = _validate_mass(mass)
        self._charge = _validate_charge(charge)

        self._validate_expression_parameters()

    @property
    def charge(self):
        return self._charge

    @charge.setter
    def charge(self, val):
        self._charge = _validate_charge(val)

    @property
    def mass(self):
        return self._mass

    @mass.setter
    def mass(self, val):
        self._mass = _validate_mass(val)

    def __eq__(self, other):
        return hash(self) == hash(other)

    def __hash__(self):
        return hash(
            tuple(
                (
                    self.name,
                    unyt_to_hashable(self.mass),
                    unyt_to_hashable(self.charge),
                    self.expression,
                    tuple(self.independent_variables),
                    tuple(self.parameters.keys()),
                    tuple(unyt_to_hashable(val) for val in self.parameters.values())
                )
            )
        )
    def __repr__(self):
        desc = "<AtomType {}, id {}>".format(self._name, id(self))
        return desc


def _validate_charge(charge):
    if not isinstance(charge, u.unyt_array):
        warnings.warn("Charges are assumed to be elementary charge")
        charge *= u.elementary_charge
    elif charge.units.dimensions != u.elementary_charge.units.dimensions:
        warnings.warn("Charges are assumed to be elementary charge")
        charge = charge.value * u.elementary_charge
    else:
        pass

    return charge


def _validate_mass(mass):
    if not isinstance(mass, u.unyt_array):
        warnings.warn("Masses are assumed to be g/mol")
        mass *= u.gram / u.mol
    elif mass.units.dimensions != (u.gram / u.mol).units.dimensions:
        warnings.warn("Masses are assumed to be g/mol")
        mass = mass.value * u.gram / u.mol
    else:
        pass

    return mass
