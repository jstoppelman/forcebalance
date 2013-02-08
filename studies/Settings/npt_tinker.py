#!/usr/bin/env python

"""
@package npt_tinker

NPT simulation in TINKER.  Runs a simulation to compute bulk properties
(for example, the density or the enthalpy of vaporization) and compute the
derivative with respect to changing the force field parameters.

The basic idea is this: First we run a density simulation to determine
the average density.  This quantity of course has some uncertainty,
and in general we want to avoid evaluating finite-difference
derivatives of noisy quantities.  The key is to realize that the
densities are sampled from a Boltzmann distribution, so the analytic
derivative can be computed if the potential energy derivative is
accessible.  We compute the potential energy derivative using
finite-difference of snapshot energies and apply a simple formula to
compute the density derivative.

This script borrows from John Chodera's ideal gas simulation in PyOpenMM.

References

[1] Shirts MR, Mobley DL, Chodera JD, and Pande VS. Accurate and efficient corrections for
missing dispersion interactions in molecular simulations. JPC B 111:13052, 2007.

[2] Ahn S and Fessler JA. Standard errors of mean, variance, and standard deviation estimators.
Technical Report, EECS Department, The University of Michigan, 2003.

Copyright And License

@author Lee-Ping Wang <leeping@stanford.edu>
@author John D. Chodera <jchodera@gmail.com>

All code in this repository is released under the GNU Lesser General Public License.

This program is distributed in the hope that it will be useful, but without any
warranty; without even the implied warranty of merchantability or fitness for a
particular purpose.  See the GNU Lesser General Public License for more details.

You should have received a copy of the GNU Lesser General Public License along with
this program.  If not, see <http://www.gnu.org/licenses/>.

"""

#================#
# Global Imports #
#================#

import os
import sys
import numpy as np
from forcebalance.forcefield import FF
from forcebalance.nifty import col, flat, lp_dump, lp_load, printcool, printcool_dictionary, _exec
from forcebalance.finite_difference import fdwrap, f1d2p, f12d3p, f1d7p
from forcebalance.molecule import Molecule
import argparse

#======================================================#
# Global, user-tunable variables (simulation settings) #
#======================================================#

# Select run parameters
# For consistency with the OpenMM module, the run length is set in this script.
timestep = 0.50                      # timestep for integration in femtosecond
nsteps = 200                         # one "iteration" = one interval for saving coordinates (in steps)
nequiliterations = 500               # number of equilibration iterations
niterations = 2000                   # number of production iterations
keyfile = None

parser = argparse.ArgumentParser()
parser.add_argument('xyzfile')
parser.add_argument('-k', '--keyfile')
parser.add_argument('temperature',type=float)
parser.add_argument('pressure',type=float)
args = parser.parse_args()

temperature = args.temperature
pressure    = args.pressure

# Flag to set verbose debug output
verbose = True

def statisticalInefficiency(A_n, B_n=None, fast=False, mintime=3):

    """
    Compute the (cross) statistical inefficiency of (two) timeseries.

    Notes
      The same timeseries can be used for both A_n and B_n to get the autocorrelation statistical inefficiency.
      The fast method described in Ref [1] is used to compute g.

    References
      [1] J. D. Chodera, W. C. Swope, J. W. Pitera, C. Seok, and K. A. Dill. Use of the weighted
      histogram analysis method for the analysis of simulated and parallel tempering simulations.
      JCTC 3(1):26-41, 2007.

    Examples

    Compute statistical inefficiency of timeseries data with known correlation time.

    >>> import timeseries
    >>> A_n = timeseries.generateCorrelatedTimeseries(N=100000, tau=5.0)
    >>> g = statisticalInefficiency(A_n, fast=True)

    @param[in] A_n (required, numpy array) - A_n[n] is nth value of
    timeseries A.  Length is deduced from vector.

    @param[in] B_n (optional, numpy array) - B_n[n] is nth value of
    timeseries B.  Length is deduced from vector.  If supplied, the
    cross-correlation of timeseries A and B will be estimated instead of
    the autocorrelation of timeseries A.

    @param[in] fast (optional, boolean) - if True, will use faster (but
    less accurate) method to estimate correlation time, described in
    Ref. [1] (default: False)

    @param[in] mintime (optional, int) - minimum amount of correlation
    function to compute (default: 3) The algorithm terminates after
    computing the correlation time out to mintime when the correlation
    function furst goes negative.  Note that this time may need to be
    increased if there is a strong initial negative peak in the
    correlation function.

    @return g The estimated statistical inefficiency (equal to 1 + 2
    tau, where tau is the correlation time).  We enforce g >= 1.0.

    """

    # Create numpy copies of input arguments.
    A_n = np.array(A_n)
    if B_n is not None:
        B_n = np.array(B_n)
    else:
        B_n = np.array(A_n)
    # Get the length of the timeseries.
    N = A_n.size
    # Be sure A_n and B_n have the same dimensions.
    if(A_n.shape != B_n.shape):
        raise ParameterError('A_n and B_n must have same dimensions.')
    # Initialize statistical inefficiency estimate with uncorrelated value.
    g = 1.0
    # Compute mean of each timeseries.
    mu_A = A_n.mean()
    mu_B = B_n.mean()
    # Make temporary copies of fluctuation from mean.
    dA_n = A_n.astype(np.float64) - mu_A
    dB_n = B_n.astype(np.float64) - mu_B
    # Compute estimator of covariance of (A,B) using estimator that will ensure C(0) = 1.
    sigma2_AB = (dA_n * dB_n).mean() # standard estimator to ensure C(0) = 1
    # Trap the case where this covariance is zero, and we cannot proceed.
    if(sigma2_AB == 0):
        print 'Sample covariance sigma_AB^2 = 0 -- cannot compute statistical inefficiency'
        return 1.0
    # Accumulate the integrated correlation time by computing the normalized correlation time at
    # increasing values of t.  Stop accumulating if the correlation function goes negative, since
    # this is unlikely to occur unless the correlation function has decayed to the point where it
    # is dominated by noise and indistinguishable from zero.
    t = 1
    increment = 1
    while (t < N-1):
        # compute normalized fluctuation correlation function at time t
        C = sum( dA_n[0:(N-t)]*dB_n[t:N] + dB_n[0:(N-t)]*dA_n[t:N] ) / (2.0 * float(N-t) * sigma2_AB)
        # Terminate if the correlation function has crossed zero and we've computed the correlation
        # function at least out to 'mintime'.
        if (C <= 0.0) and (t > mintime):
            break
        # Accumulate contribution to the statistical inefficiency.
        g += 2.0 * C * (1.0 - float(t)/float(N)) * float(increment)
        # Increment t and the amount by which we increment t.
        t += increment
        # Increase the interval if "fast mode" is on.
        if fast: increment += 1
    # g must be at least unity
    if (g < 1.0): g = 1.0
    # Return the computed statistical inefficiency.
    return g

def run_simulation(xyz, tky, tstep, nstep, neq, npr, pbc=True):
    """ Run a NPT simulation and gather statistics. """

    basename = xyz[:-4]
    xin = "%s" % xyz + ("" if tky == None else " -k %s" % tky)
    xain = "%s.arc" % basename + ("" if tky == None else " -k %s" % tky)
    
    cmdstr = "./minimize %s 1.0e-1" % xin
    _exec(cmdstr,print_to_screen=True)
    _exec("mv %s_2 %s" % (xyz,xyz),print_to_screen=True)
    # Run the equilibration.
    if pbc:
        cmdstr = "./dynamic %s %i %f %f 4 %f %f" % (xin, nstep*neq, tstep, nstep*tstep/1000, temperature, pressure)
    else:
        cmdstr = "./dynamic %s %i %f %f 2 %f" % (xin, nstep*neq, tstep, nstep*tstep/1000, temperature)
    _exec(cmdstr,print_to_screen=True)
    _exec("rm -f %s.arc %s.box" % (basename, basename), print_to_screen=True)
    # Run the production.
    if pbc:
        cmdstr = "./dynamic %s %i %f %f 4 %f %f" % (xin, nstep*npr, tstep, nstep*tstep/1000, temperature, pressure)
    else:
        cmdstr = "./dynamic %s %i %f %f 2 %f" % (xin, nstep*npr, tstep, nstep*tstep/1000, temperature)
    odyn = _exec(cmdstr,print_to_screen=True)

    edyn = []
    for line in odyn.split('\n'):
        if 'Current Potential' in line:
            edyn.append(float(line.split()[2]))

    edyn = np.array(edyn) * 4.184

    cmdstr = "./analyze %s" % xain
    oanl = _exec(cmdstr,stdin="G,E")

    # Read potential energy and dipole from file.
    eanl = []
    dip = []
    mass = 0.0
    for line in oanl.split('\n'):
        if 'Total System Mass' in line:
            mass = float(line.split()[-1])
        if 'Total Potential Energy : ' in line:
            eanl.append(float(line.split()[4]))
        if 'Dipole X,Y,Z-Components :' in line:
            dip.append([float(line.split()[i]) for i in range(-3,0)])

    # Energies in kilojoules per mole
    eanl = np.array(eanl) * 4.184
    # Dipole moments in debye
    dip = np.array(dip)
    # Volume of simulation boxes in cubic nanometers
    # Conversion factor derived from the following:
    # In [22]: 1.0 * gram / mole / (1.0 * nanometer)**3 / AVOGADRO_CONSTANT_NA / (kilogram/meter**3)
    # Out[22]: 1.6605387831627252
    conv = 1.6605387831627252
    if pbc:
        box = [[float(i) for i in line.split()[1:4]] for line in open(xyz[:-3]+"box").readlines()]
        vol = np.array([i[0]*i[1]*i[2] for i in box]) / 1000
        rho = conv * mass / vol
    else:
        vol = None
        rho = None

    return rho, edyn, vol, dip



    # # Analyze.
    # cmdstr = "./analyze %s" % xain
    # oanl = _exec(cmdstr,print_to_screen=True,stdin="E,M")

    # dyne = []
    # for line in odyn.split('\n'):
    #     if 'Current Potential' in line:
    #         print line
    #         dyne.append(float(line.split()[2]))
    # anle = []
    # for line in oanl.split('\n'):
    #     if 'Total Potential Energy : ' in line:
    #         print line
    #         anle.append(float(line.split()[4]))

    # anle = np.array(anle)
    # # Convert to kJ/mol
    # anle *= 4.184 

    # sys.exit()
    # # More data structures; stored coordinates, box sizes, densities, and potential energies
    # rhos = []
    # energies = []
    # volumes = []
    # dipoles = []
    # return

def energy_driver(mvals,FF,xyz,tky,verbose=False,dipole=False):
    """
    Compute a set of snapshot energies (and optionally, dipoles) as a function of the force field parameters.

    ForceBalance creates the force field, TINKER reads it in, and we loop through the snapshots
    to compute the energies.

    @param[in] mvals Mathematical parameter values
    @param[in] FF ForceBalance force field object
    @return E A numpy array of energies in kilojoules per mole

    """
    # Part of the command line argument to TINKER.
    basename = xyz[:-4]
    xin = "%s" % xyz + ("" if tky == None else " -k %s" % tky)
    xain = "%s.arc" % basename + ("" if tky == None else " -k %s" % tky)
    
    # Print the force field file from the ForceBalance object, with modified parameters.
    FF.make(mvals)
    
    # Execute TINKER.
    cmdstr = "./analyze %s" % xain
    oanl = _exec(cmdstr,stdin="E",print_command=False)

    # Read potential energy from file.
    E = []
    for line in oanl.split('\n'):
        if 'Total Potential Energy : ' in line:
            E.append(float(line.split()[4]))
    E = np.array(E) * 4.184
    if dipole:
        # If desired, read dipole from file.
        D = []
        for line in oanl.split('\n'):
            if 'Dipole X,Y,Z-Components :' in line:
                D.append([float(line.split()[i]) for i in range(-3,0)])
        D = np.array(D)
        # Return a Nx4 array with energies in the first column and dipole in columns 2-4.
        answer = np.hstack((E.reshape(-1,1), D.reshape(-1,3)))
        return answer
    else:
        return E

def energy_derivatives(mvals,h,FF,xyz,tky,AGrad=True):

    """
    Compute the first and second derivatives of a set of snapshot
    energies with respect to the force field parameters.

    This basically calls the finite difference subroutine on the
    energy_driver subroutine also in this script.

    @todo This is a sufficiently general function to be merged into openmmio.py?
    @param[in] mvals Mathematical parameter values
    @param[in] pdb OpenMM PDB object
    @param[in] FF ForceBalance force field object
    @param[in] xyzs List of OpenMM positions
    @param[in] settings OpenMM settings for creating the System
    @param[in] boxes Periodic box vectors
    @return G First derivative of the energies in a N_param x N_coord array

    """
    E0       = energy_driver(mvals, FF, xyz, tky)
    ns       = len(E0)
    G        = np.zeros((FF.np,ns))
    if not AGrad:
        return G
    CheckFDPts = False
    for i in range(FF.np):
        G[i,:], _ = f12d3p(fdwrap(energy_driver,mvals,i,FF=FF,xyz=xyz,tky=tky),h,f0=E0)
        if CheckFDPts:
            # Check whether the number of finite difference points is sufficient.  Forward difference still gives residual error of a few percent.
            G1 = f1d7p(fdwrap(energy_driver,mvals,i,FF=FF,xyz=xyz,tky=tky),h)
            dG = G1 - G[i,:]
            dGfrac = (G1 - G[i,:]) / G[i,:]
            print "Parameter %3i 7-pt vs. central derivative : RMS, Max error (fractional) = % .4e % .4e (% .4e % .4e)" % (i, np.sqrt(np.mean(dG**2)), max(np.abs(dG)), np.sqrt(np.mean(dGfrac**2)), max(np.abs(dGfrac)))
    return G

def energy_dipole_derivatives(mvals,h,FF,xyz,tky,AGrad=True):

    """
    Compute the first and second derivatives of a set of snapshot
    energies with respect to the force field parameters.

    This basically calls the finite difference subroutine on the
    energy_driver subroutine also in this script.

    @todo This is a sufficiently general function to be merged into openmmio.py?
    @param[in] mvals Mathematical parameter values
    @param[in] pdb OpenMM PDB object
    @param[in] FF ForceBalance force field object
    @param[in] xyzs List of OpenMM positions
    @param[in] settings OpenMM settings for creating the System
    @param[in] boxes Periodic box vectors
    @return G First derivative of the energies in a N_param x N_coord array

    """
    ED0      = energy_driver(mvals, FF, xyz=xyz, tky=tky, dipole=True)
    ns       = ED0.shape[0]
    G        = np.zeros((FF.np,ns))
    GDx      = np.zeros((FF.np,ns))
    GDy      = np.zeros((FF.np,ns))
    GDz      = np.zeros((FF.np,ns))
    if not AGrad:
        return G, GDx, GDy, GDz
    CheckFDPts = False
    for i in range(FF.np):
        EDG, _   = f12d3p(fdwrap(energy_driver,mvals,i,FF=FF,xyz=xyz,tky=tky,dipole=True),h,f0=ED0)
        G[i,:]   = EDG[:,0]
        GDx[i,:] = EDG[:,1]
        GDy[i,:] = EDG[:,2]
        GDz[i,:] = EDG[:,3]
    return G, GDx, GDy, GDz

def bzavg(obs,boltz):
    if obs.ndim == 2:
        if obs.shape[0] == len(boltz) and obs.shape[1] == len(boltz):
            raise Exception('Error - both dimensions have length equal to number of snapshots, now confused!')
        elif obs.shape[0] == len(boltz):
            return np.sum(obs*boltz.reshape(-1,1),axis=0)/np.sum(boltz)
        elif obs.shape[1] == len(boltz):
            return np.sum(obs*boltz,axis=1)/np.sum(boltz)
        else:
            raise Exception('The dimensions are wrong!')
    elif obs.ndim == 1:
        return np.dot(obs,boltz)/sum(boltz)
    else:
        raise Exception('The number of dimensions can only be 1 or 2!')

def property_derivatives(mvals,h,FF,xyz,tky,kT,property_driver,property_kwargs,AGrad=True):
    G        = np.zeros(FF.np)
    if not AGrad:
        return G
    ED0      = energy_driver(mvals, FF, xyz=xyz, tky=tky, dipole=True)
    E0       = ED0[:,0]
    D0       = ED0[:,1:]
    P0       = property_driver(b=None, **property_kwargs)
    if 'h_' in property_kwargs:
        H0 = property_kwargs['h_'].copy()

    for i in range(FF.np):
        ED1 = fdwrap(energy_driver,mvals,i,FF=FF,xyz=xyz,tky=tky,dipole=True)(h)
        E1       = ED1[:,0]
        D1       = ED1[:,1:]
        b = np.exp(-(E1-E0)/kT)
        b /= np.sum(b)
        if 'h_' in property_kwargs:
            property_kwargs['h_'] = H0.copy() + (E1-E0)
        if 'd_' in property_kwargs:
            property_kwargs['d_'] = D1.copy()
        S = -1*np.dot(b,np.log(b))
        InfoContent = np.exp(S)
        if InfoContent / len(E0) < 0.1:
            print "Warning: Effective number of snapshots: % .1f (out of %i)" % (InfoContent, len(E0))
        P1 = property_driver(b, **property_kwargs)

        EDM1 = fdwrap(energy_driver,mvals,i,FF=FF,xyz=xyz,tky=tky,dipole=True)(-h)
        EM1       = EDM1[:,0]
        DM1       = EDM1[:,1:]
        b = np.exp(-(EM1-E0)/kT)
        b /= np.sum(b)
        if 'h_' in property_kwargs:
            property_kwargs['h_'] = H0.copy() + (EM1-E0)
        if 'd_' in property_kwargs:
            property_kwargs['d_'] = DM1.copy()
        S = -1*np.dot(b,np.log(b))
        InfoContent = np.exp(S)
        if InfoContent / len(E0) < 0.1:
            print "Warning: Effective number of snapshots: % .1f (out of %i)" % (InfoContent, len(E0))
        PM1 = property_driver(b, **property_kwargs)

        G[i] = (P1-PM1)/(2*h)

    if 'h_' in property_kwargs:
        property_kwargs['h_'] = H0.copy()
    if 'd_' in property_kwargs:
        property_kwargs['d_'] = D0.copy()

    return G

def main():

    """
    Usage: (runcuda.sh) npt.py input.xyz [-k input.key] <temperature> <pressure>
    """
    
    # Set up some conversion factors
    # All units are in kJ/mol
    N = niterations
    # Conversion factor for kT derived from:
    # In [6]: 1.0 / ((1.0 * kelvin * BOLTZMANN_CONSTANT_kB * AVOGADRO_CONSTANT_NA) / kilojoule_per_mole)
    # Out[6]: 120.27221251395186
    T     = temperature
    mBeta = -120.27221251395186 / temperature
    Beta  =  120.27221251395186 / temperature
    kT    =  0.0083144724712202 * temperature
    # Conversion factor for pV derived from:
    # In [14]: 1.0 * atmosphere * nanometer ** 3 * AVOGADRO_CONSTANT_NA / kilojoule_per_mole
    # Out[14]: 0.061019351687175
    pcon  =  0.061019351687175

    # Load the force field in from the ForceBalance pickle.
    FF,mvals,h,AGrad = lp_load(open('forcebalance.p'))
    
    # Create the force field XML files.
    FF.make(mvals)

    #=================================================================#
    # Run the simulation for the full system and analyze the results. #
    #=================================================================#
    Rhos, Energies, Volumes, Dips = run_simulation(args.xyzfile,args.keyfile,tstep=timestep,nstep=nsteps,neq=nequiliterations,npr=niterations)
    V  = Volumes
    pV = pressure * Volumes
    H = Energies + pV

    # Get the energy and dipole gradients.
    print "Post-processing the liquid simulation snapshots."
    G, GDx, GDy, GDz = energy_dipole_derivatives(mvals,h,FF,args.xyzfile,args.keyfile,AGrad)
    print

    #==============================================#
    # Now run the simulation for just the monomer. #
    #==============================================#
    _a, mEnergies, _b, _c = run_simulation("mono.xyz","mono.key",tstep=0.10,nstep=100,neq=50,npr=10000,pbc=False)
    mN = len(mEnergies)
    print "Post-processing the gas simulation snapshots."
    mG = energy_derivatives(mvals,h,FF,"mono.xyz","mono.key",AGrad)
    print

    numboots = 1000    
    def bootstats(func,inputs):
        # Calculate error using bootstats method
        dboot = []
        for i in range(numboots):
            newins = {k : v[np.random.randint(len(v),size=len(v))] for k,v in inputs.items()}
            dboot.append(np.mean(func(**newins)))
        return func(**inputs),np.std(np.array(dboot))
        
    def calc_arr(b = None, **kwargs):
        # This tomfoolery is required because of Python syntax;
        # default arguments must come after nondefault arguments
        # and kwargs must come at the end.  This function is used
        # in bootstrap error calcs and also in derivative calcs.
        if 'arr' in kwargs:
            arr = kwargs['arr']
        if b == None: b = np.ones(len(arr),dtype=float)
        return bzavg(arr,b)

    # The density in kg/m^3.
    # Note: Not really necessary to use bootstrap here, but good to 
    # demonstrate the principle.
    Rho_avg,  Rho_err  = bootstats(calc_arr,{'arr':Rhos})
    Rho_err *= np.sqrt(statisticalInefficiency(Rhos))

    print "The finite difference step size is:",h

    # The first density derivative
    GRho = mBeta * (flat(np.mat(G) * col(Rhos)) / N - np.mean(Rhos) * np.mean(G, axis=1))

    FDCheck = False

    if FDCheck:
        Sep = printcool("Numerical Derivative:")
        GRho1 = property_derivatives(mvals, h, FF, args.xyzfile, args.keyfile, kT, calc_arr, {'arr':Rhos})
        FF.print_map(vals=GRho1)
        Sep = printcool("Difference (Absolute, Fractional):")
        absfrac = ["% .4e  % .4e" % (i-j, (i-j)/j) for i,j in zip(GRho, GRho1)]
        FF.print_map(vals=absfrac)

    # The enthalpy of vaporization in kJ/mol.
    Pot_avg,  Pot_err  = bootstats(calc_arr,{'arr':Energies})
    mPot_avg, mPot_err = bootstats(calc_arr,{'arr':mEnergies})
    pV_avg,   pV_err   = bootstats(calc_arr,{'arr':pV})
    Pot_err  *= np.sqrt(statisticalInefficiency(Energies))
    mPot_err *= np.sqrt(statisticalInefficiency(mEnergies))
    pV_err   *= np.sqrt(statisticalInefficiency(pV))

    Hvap_avg = mPot_avg - Pot_avg / 216 + kT - np.mean(pV) / 216
    Hvap_err = np.sqrt(Pot_err**2 / 216**2 + mPot_err**2 + pV_err**2/216**2)

    # Build the first Hvap derivative.
    GHvap = np.mean(G,axis=1)
    GHvap += mBeta * (flat(np.mat(G) * col(Energies)) / N - Pot_avg * np.mean(G, axis=1))
    GHvap /= 216
    GHvap -= np.mean(mG,axis=1)
    GHvap -= mBeta * (flat(np.mat(mG) * col(mEnergies)) / N - mPot_avg * np.mean(mG, axis=1))
    GHvap *= -1
    GHvap -= mBeta * (flat(np.mat(G) * col(pV)) / N - np.mean(pV) * np.mean(G, axis=1)) / 216

    print "Box energy:", np.mean(Energies)
    print "Monomer energy:", np.mean(mEnergies)
    Sep = printcool("Enthalpy of Vaporization: % .4f +- %.4f kJ/mol, Derivatives below" % (Hvap_avg, Hvap_err))
    FF.print_map(vals=GHvap)
    print Sep

    # Define some things to make the analytic derivatives easier.
    Gbar = np.mean(G,axis=1)
    def covde(vec):
        return flat(np.mat(G)*col(vec))/N - Gbar*np.mean(vec)
    def avg(vec):
        return np.mean(vec)

    ## Thermal expansion coefficient and bootstrap error estimation
    def calc_alpha(b = None, **kwargs):
        if 'h_' in kwargs:
            h_ = kwargs['h_']
        if 'v_' in kwargs:
            v_ = kwargs['v_']
        if b == None: b = np.ones(len(v_),dtype=float)
        return 1/(kT*T) * (bzavg(h_*v_,b)-bzavg(h_,b)*bzavg(v_,b))/bzavg(v_,b)

    Alpha, Alpha_err = bootstats(calc_alpha,{'h_':H, 'v_':V})
    Alpha_err *= np.sqrt(max(statisticalInefficiency(V),statisticalInefficiency(H)))

    ## Thermal expansion coefficient analytic derivative
    GAlpha1 = mBeta * covde(H*V) / avg(V)
    GAlpha2 = Beta * avg(H*V) * covde(V) / avg(V)**2
    GAlpha3 = flat(np.mat(G)*col(V))/N/avg(V) - Gbar
    GAlpha4 = Beta * covde(H)
    GAlpha  = (GAlpha1 + GAlpha2 + GAlpha3 + GAlpha4)/(kT*T)
    Sep = printcool("Thermal expansion coefficient: % .4e +- %.4e K^-1\nAnalytic Derivative:" % (Alpha, Alpha_err))
    FF.print_map(vals=GAlpha)
    if FDCheck:
        GAlpha_fd = property_derivatives(mvals, h, FF, args.xyzfile, args.keyfile, kT, calc_alpha, {'h_':H,'v_':V})
        Sep = printcool("Numerical Derivative:")
        FF.print_map(vals=GAlpha_fd)
        Sep = printcool("Difference (Absolute, Fractional):")
        absfrac = ["% .4e  % .4e" % (i-j, (i-j)/j) for i,j in zip(GAlpha, GAlpha_fd)]
        FF.print_map(vals=absfrac)

    ## Isothermal compressibility
    # In [15]: 1.0*bar*nanometer**3/kilojoules_per_mole/item
    # Out[15]: 0.06022141792999999

    bar_unit = 0.06022141793
    def calc_kappa(b=None, **kwargs):
        if 'v_' in kwargs:
            v_ = kwargs['v_']
        if b == None: b = np.ones(len(v_),dtype=float)
        return bar_unit / kT * (bzavg(v_**2,b)-bzavg(v_,b)**2)/bzavg(v_,b)

    Kappa, Kappa_err = bootstats(calc_kappa,{'v_':V})
    Kappa_err *= np.sqrt(statisticalInefficiency(V))

    ## Isothermal compressibility analytic derivative
    Sep = printcool("Isothermal compressibility:    % .4e +- %.4e bar^-1\nAnalytic Derivative:" % (Kappa, Kappa_err))
    GKappa1 = -1 * Beta**2 * avg(V) * covde(V**2) / avg(V)**2
    GKappa2 = +1 * Beta**2 * avg(V**2) * covde(V) / avg(V)**2
    GKappa3 = +1 * Beta**2 * covde(V)
    GKappa  = bar_unit*(GKappa1 + GKappa2 + GKappa3)
    FF.print_map(vals=GKappa)
    if FDCheck:
        GKappa_fd = property_derivatives(mvals, h, FF, args.xyzfile, args.keyfile, kT, calc_kappa, {'v_':V})
        Sep = printcool("Numerical Derivative:")
        FF.print_map(vals=GKappa_fd)
        Sep = printcool("Difference (Absolute, Fractional):")
        absfrac = ["% .4e  % .4e" % (i-j, (i-j)/j) for i,j in zip(GKappa, GKappa_fd)]
        FF.print_map(vals=absfrac)

    ## Isobaric heat capacity
    def calc_cp(b=None, **kwargs):
        if 'h_' in kwargs:
            h_ = kwargs['h_']
        if b == None: b = np.ones(len(h_),dtype=float)
        Cp_  = 1/(216*kT*T) * (bzavg(h_**2,b) - bzavg(h_,b)**2)
        Cp_ *= 1000 / 4.184
        return Cp_

    Cp, Cp_err = bootstats(calc_cp, {'h_':H})
    Cp_err *= np.sqrt(statisticalInefficiency(H))

    ## Isobaric heat capacity analytic derivative
    GCp1 = 2*covde(H) * 1000 / 4.184 / (216*kT*T)
    GCp2 = mBeta*covde(H**2) * 1000 / 4.184 / (216*kT*T)
    GCp3 = 2*Beta*avg(H)*covde(H) * 1000 / 4.184 / (216*kT*T)
    GCp  = GCp1 + GCp2 + GCp3
    Sep = printcool("Isobaric heat capacity:        % .4e +- %.4e cal mol-1 K-1\nAnalytic Derivative:" % (Cp, Cp_err))
    FF.print_map(vals=GCp)
    if FDCheck:
        GCp_fd = property_derivatives(mvals, h, FF, args.xyzfile, args.keyfile, kT, calc_cp, {'h_':H})
        Sep = printcool("Numerical Derivative:")
        FF.print_map(vals=GCp_fd)
        Sep = printcool("Difference (Absolute, Fractional):")
        absfrac = ["% .4e  % .4e" % (i-j, (i-j)/j) for i,j in zip(GCp,GCp_fd)]
        FF.print_map(vals=absfrac)

    ## Dielectric constant
    # eps0 = 8.854187817620e-12 * coulomb**2 / newton / meter**2
    # epsunit = 1.0*(debye**2) / nanometer**3 / BOLTZMANN_CONSTANT_kB / kelvin
    # prefactor = epsunit/eps0/3
    prefactor = 30.348705333964077
    def calc_eps0(b=None, **kwargs):
        if 'd_' in kwargs: # Dipole moment vector.
            d_ = kwargs['d_']
        if 'v_' in kwargs: # Volume.
            v_ = kwargs['v_']
        if b == None: b = np.ones(len(v_),dtype=float)
        dx = d_[:,0]
        dy = d_[:,1]
        dz = d_[:,2]
        D2  = bzavg(dx**2,b)-bzavg(dx,b)**2
        D2 += bzavg(dy**2,b)-bzavg(dy,b)**2
        D2 += bzavg(dz**2,b)-bzavg(dz,b)**2
        return prefactor*D2/bzavg(v_,b)/T

    Eps0, Eps0_err = bootstats(calc_eps0,{'d_':Dips, 'v_':V})
    Eps0_err *= np.sqrt(np.mean([statisticalInefficiency(Dips[:,0]),statisticalInefficiency(Dips[:,1]),statisticalInefficiency(Dips[:,2])]))

    ## Dielectric constant analytic derivative
    Dx = Dips[:,0]
    Dy = Dips[:,1]
    Dz = Dips[:,2]
    D2 = avg(Dx**2)+avg(Dy**2)+avg(Dz**2)-avg(Dx)**2-avg(Dy)**2-avg(Dz)**2
    GD2  = 2*(flat(np.mat(GDx)*col(Dx))/N - avg(Dx)*(np.mean(GDx,axis=1))) - Beta*(covde(Dx**2) - 2*avg(Dx)*covde(Dx))
    GD2 += 2*(flat(np.mat(GDy)*col(Dy))/N - avg(Dy)*(np.mean(GDy,axis=1))) - Beta*(covde(Dy**2) - 2*avg(Dy)*covde(Dy))
    GD2 += 2*(flat(np.mat(GDz)*col(Dz))/N - avg(Dz)*(np.mean(GDz,axis=1))) - Beta*(covde(Dz**2) - 2*avg(Dz)*covde(Dz))
    GEps0 = prefactor*(GD2/avg(V) - mBeta*covde(V)*D2/avg(V)**2)/T
    Sep = printcool("Dielectric constant:           % .4e +- %.4e\nAnalytic Derivative:" % (Eps0, Eps0_err))
    FF.print_map(vals=GEps0)
    if FDCheck:
        GEps0_fd = property_derivatives(mvals, h, FF, args.xyzfile, args.keyfile, kT, calc_eps0, {'d_':Dips,'v_':V})
        Sep = printcool("Numerical Derivative:")
        FF.print_map(vals=GEps0_fd)
        Sep = printcool("Difference (Absolute, Fractional):")
        absfrac = ["% .4e  % .4e" % (i-j, (i-j)/j) for i,j in zip(GEps0,GEps0_fd)]
        FF.print_map(vals=absfrac)

    ## Print the final force field.
    pvals = FF.make(mvals)

    with open(os.path.join('npt_result.p'),'w') as f: lp_dump((Rhos, Volumes, Energies, Dips, G, [GDx, GDy, GDz], mEnergies, mG, Rho_err, Hvap_err, Alpha_err, Kappa_err, Cp_err, Eps0_err),f)

if __name__ == "__main__":
    main()
