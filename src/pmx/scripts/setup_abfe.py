#! /usr/bin/env python

from __future__ import print_function, division
import argparse
from pmx.model import Model, merge_models, assign_masses_to_model
from pmx.alchemy import AbsRestraints
from pmx.forcefield import Topology, merge_atomtypes
from pmx import gmx
from copy import deepcopy
import os
import shutil

# TODO: allow providing indices for restraint
# TODO: build systems with pmx.gmx
# TODO: build folder structure and mdp files for equil or nonequil calcs

"""
Script to setup absolute binding free energy calculations for ligands binding to proteins.
"""

def parse_options():
    parser = argparse.ArgumentParser(description='''
describe...
''')
    parser.add_argument('-pt',
                        metavar='protop',
                        dest='pro_top',
                        type=str,
                        help='Input topology file for the protein. '
                        'Default is "protein.top".',
                        default='protein.top')
    parser.add_argument('-lt',
                        metavar='ligtop',
                        dest='lig_top',
                        type=str,
                        help='Input topology file for the ligand. '
                        'Default is "ligand.itp".',
                        default='ligand.itp')
    parser.add_argument('-pc',
                        metavar='procrd',
                        dest='pro_crd',
                        type=str,
                        help='Input structure file in PDB or GRO format '
                        'for the protein. Default is "protein.gro".',
                        default='protein.gro')
    parser.add_argument('-lc',
                        metavar='ligcrd',
                        dest='lig_crd',
                        type=str,
                        help='Input structure file in PDB or GRO format '
                        'for the ligand. Default is "ligand.gro".',
                        default='ligand.gro')
    parser.add_argument('--build',
                        dest='build',
                        help='Whether to build the system (editconf, solvate, '
                        'genion) with a standard setup once the input files '
                        '(top, gro) are ready.',
                        default=False,
                        action='store_true')
    parser.add_argument('--singlebox',
                        dest='singlebox',
                        help='Whether to use the double-system single-box '
                        'setup.',
                        default=False,
                        action='store_true')
    parser.add_argument('--seed',
                        metavar='int',
                        dest='seed',
                        help='Random seed to use when picking atoms for the '
                        'restraints. The automated restraints selection is '
                        'stochastic, so if you want to have a reproducible '
                        'behaviour, provide a random seed.',
                        default=None,
                        type=int)

    args, unknown = parser.parse_known_args()

    return args


def main(args):

    # Import GRO and TOP files
    lig = Model(args.lig_crd, renumber_residues=False)
    pro = Model(args.pro_crd, renumber_residues=False)
    ligtop = Topology(args.lig_top, is_itp=True, assign_types=False)
    protop = Topology(args.pro_top, assign_types=False)

    # check lig has only 1 residue
    assert len(lig.residues) == 1

    # assign masses to ligand atoms
    assign_masses_to_model(lig, ligtop)

    # add position restraints to ligand topology if missing
    if not ligtop.footer and ligtop.has_posre is False:
        ligtop.make_posre()

    # ----------------------
    # merge gro into complex
    # ----------------------
    com = merge_models(lig, pro)
    com.box = pro.box
    com.renumber_atoms()

    # ------------------------
    # make topology of complex
    # ------------------------
    comtop = deepcopy(protop)
    comtop.header.append('#include "ligand.itp"')
    comtop.system = 'Complex'
    comtop.molecules.insert(0, [ligtop.name, 1])

    # merge atomtypes
    comtop.atomtypes = merge_atomtypes(ligtop.atomtypes, protop.atomtypes)

    # ------------------------------------------
    # figure out restraints (and save top entry)
    # ------------------------------------------
    restraints = AbsRestraints(pro, lig, seed=args.seed)

    # write restraints info
    restraints.write_summary()

    if args.build is False:
        # write restraints section only if we are not setting up the system
        # solvate/genion do not work well with topologies that contain
        # this restraints section
        comtop.ii = restraints.make_ii()

    # -------------------------------
    # save coordinates/topology files
    # -------------------------------
    com.write('complex.gro')
    ligtop.write('ligand.itp', stateBonded='A', write_atypes=False, posre_include=True)
    comtop.write('complex.top', stateBonded='A')

    # ---------------------------------------------------
    # gmx setup: editconf, solvate, genion, mdp files etc
    # ---------------------------------------------------
    if args.build is True:
        # ----------------
        # single-box setup
        # ----------------
        if args.singlebox is True:
            pass

        # ----------------------------------
        # standard setup with separate boxes
        # ----------------------------------
        elif args.singlebox is False:

            # Setup complex
            # -------------
            os.mkdir('complex')
            os.chdir('complex')

            shutil.copy('../complex.gro', '.')
            shutil.copy('../complex.top', '.')
            shutil.copy('../ligand.itp', '.')

            gmx.editconf(f='complex.gro', o='editconf.gro', bt='cubic', d=1.2)
            gmx.solvate(cp='editconf.gro', cs='spc216.gro', p='complex.top', o='solvate.gro')
            gmx.write_mdp(mdp='enmin', fout='genion.mdp')
            gmx.grompp(f='genion.mdp', c='solvate.gro', p='complex.top', o='genion.tpr', maxwarn=1)
            gmx.genion(s='genion.tpr', p='complex.top', o='genion.gro', conc=0.15, neutral=True)

            # add restraints to topology
            comtop = Topology('complex.top', assign_types=False)
            comtop.ii = restraints.make_ii()
            comtop.write('complex.top', stateBonded='A')

            os.chdir('../')

            # Setup ligand
            # ------------
            os.mkdir('ligand')
            os.chdir('ligand')

            lig.write('ligand.gro')
            # setup topology
            # use the same protein ff for the water/ions in ligand sims
            ligtop.is_itp = False  # now we want to write it as top file
            ligtop.forcefield = protop.forcefield
            ligtop.footer = ['#include "{ff}.ff/tip3p.itp"'.format(ff=ligtop.forcefield),
                             '#ifdef POSRES_WATER',
                             '[ position_restraints ]',
                             '1    1       1000       1000       1000',
                             '#endif',
                             '#include "{ff}.ff/ions.itp"'.format(ff=ligtop.forcefield)]

            ligtop.write('ligand.top', stateBonded='A', write_atypes=True,
                         posre_include=True)

            # run gromacs setup
            gmx.editconf(f='ligand.gro', o='editconf.gro', bt='cubic', d=1.2)
            gmx.solvate(cp='editconf.gro', cs='spc216.gro', p='ligand.top', o='solvate.gro')
            gmx.write_mdp(mdp='enmin', fout='genion.mdp')
            gmx.grompp(f='genion.mdp', c='solvate.gro', p='ligand.top', o='genion.tpr', maxwarn=1)
            gmx.genion(s='genion.tpr', p='ligand.top', o='genion.gro', conc=0.15, neutral=True)

            os.chdir('../')
            print('\n\n          ********** Setup Completed **********\n\n')





if __name__ == '__main__':
    args = parse_options()
    main(args)