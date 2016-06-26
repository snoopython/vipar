# coding: utf-8

'''
stat.py - Vipar's statistic module

+ developer: Akio Taniguchi (IoA, UTokyo)
+ contact: taniguchi@ioa.s.u-tokyo.ac.jp
'''

# common preamble
import os
import sys
import inspect
import numpy as np

# unique preamble
import pyfits as fits
from copy import deepcopy
from scipy.optimize import curve_fit
from scipy.ndimage import gaussian_filter


class PCA:
    def __init__(self, scan):
        scan = np.asarray(scan)
        self.U, self.d, self.Vt = np.linalg.svd(scan, full_matrices=False)

        self.eigen = self.d**2
        self.sumvariance = np.cumsum(self.eigen)
        self.sumvariance /= self.sumvariance[-1]

        for d in self.d:
            if d > self.d[0] * 1e-6:
                self.dinv = np.array([1/d])
            else:
                self.dinv = np.array([0])

    def reconstruct(self, fraction):
        npc = np.searchsorted(self.sumvariance, fraction) + 1
        scan = np.dot(self.U[:,:npc], np.dot(np.diag(self.d[:npc]), self.Vt[:npc]))
        self.npc_last = npc
        return scan


class CheckConvergence(object):
    def __init__(self, threshold=0.01):
        self.threshold = threshold
        self.p_prev = 0.0

    def __call__(self, *p_now):
        p_now = np.array(p_now)
        d_now = np.linalg.norm(p_now)
        d_diff = np.linalg.norm(p_now - self.p_prev)
        ratio = d_diff / d_now
        self.p_prev = p_now
        print(ratio)

        return ratio <= self.threshold


class Gaussian2D(object):
    def fit(self, mg_daz, mg_del, mp_data, b_0=23.0, threshold=3.0):
        '''Fit 2D Gaussian to a map.

        Args:
        - mg_daz (2D array): meshgrid of dAz [arcsec]
        - mg_del (2D array): meshgrid of dEl [arcsec]
        - mp_data (2D array): data map [arbitrary unit]
        - b_0 (float): typical beam size for initial guess [arcsec]
        - threshold (float): threshold of S/N

        Returns:
        - mp_fit (2D array): fit map [arbitrary unit]
        - hd_fit (Header): FITS header object containing the results of fit
        '''
        # preprocessing
        mp_data = deepcopy(mp_data)
        mp_data[np.isnan(mp_data)] = np.nanmedian(mp_data)

        # step 1: initial parameters (*_0)
        b_0 = float(b_0)
        bmaj_0, bmin_0, pa_0 = b_0, b_0, 0.0

        grid_daz = np.mean(np.diff(mg_daz))
        grid_del = np.mean(np.diff(mg_del))
        grid_typ = np.sqrt(grid_daz**2 + grid_del**2)
        sigma = b_0/(2*np.sqrt(np.log(2)))/grid_typ

        mp_gauss = gaussian_filter(mp_data, sigma)
        peak_pos = np.max(mp_gauss) - np.median(mp_gauss)
        peak_neg = np.median(mp_gauss) - np.min(mp_gauss)
        argfunc = np.argmax if peak_pos > peak_neg else np.argmin
        j, i = np.unravel_index(argfunc(mp_gauss), mp_gauss.shape)
        xp_0, yp_0 = mg_daz[j,i], mg_del[j,i]

        f = self.partialfunc(xp=xp_0, yp=yp_0, bmaj=bmaj_0, bmin=bmin_0, pa=pa_0)
        popt, pcov = curve_fit(f, (mg_daz, mg_del), mp_data.flatten())
        ampl_0, offset_0 = popt

        # step 2: if initial S/N<3, then stop fitting
        sd_0 = self._estimate_sd(mg_daz, mg_del, mp_data, xp_0, yp_0, bmaj_0, bmin_0)
        sn_0 = np.abs(ampl_0/sd_0)
        if sn_0 < threshold:
            mp_fit = f((mg_daz, mg_del), *popt).reshape(mp_data.shape)
            hd_fit = fits.Header()
            hd_fit['FIT_FUNC'] = 'Gaussian2D', 'fitting function'
            hd_fit['FIT_STAT'] = 'failed', 'fiting status'

            return hd_fit, mp_fit

        # step 3: iterative fit
        cc = CheckConvergence()
        while not cc(xp_0, yp_0, bmaj_0, bmin_0, pa_0, ampl_0, offset_0):
            # bmaj, bmin, pa
            f = self.partialfunc(xp=xp_0, yp=yp_0, ampl=ampl_0, offset=offset_0)
            pinit = [bmaj_0, bmin_0, pa_0]
            popt, pcov = curve_fit(f, (mg_daz, mg_del), mp_data.flatten(), pinit)
            bmaj_0, bmin_0, pa_0 = popt
            bmaj_e, bmin_e, pa_e = np.sqrt(np.diag(pcov))
            if bmaj_0 < bmin_0:
                bmaj_0, bmin_0 = bmin_0, bmaj_0
                bmaj_e, bmin_e = bmin_e, bmaj_e
                pa_0 += 90.0

            pa_0 %= 180.0

            # xp, yp
            f = self.partialfunc(bmaj=bmaj_0, bmin=bmin_0, pa=pa_0, ampl=ampl_0, offset=offset_0)
            pinit = [xp_0, yp_0]
            popt, pcov = curve_fit(f, (mg_daz, mg_del), mp_data.flatten(), pinit)
            xp_0, yp_0 = popt
            xp_e, yp_e = np.sqrt(np.diag(pcov))

            # ampl, offset
            f = self.partialfunc(xp=xp_0, yp=yp_0, bmaj=bmaj_0, bmin=bmin_0, pa=pa_0)
            pinit = [ampl_0, offset_0]
            popt, pcov = curve_fit(f, (mg_daz, mg_del), mp_data.flatten(), pinit)
            ampl_0, offset_0 = popt
            ampl_e, offset_e = np.sqrt(np.diag(pcov))

        # step 4: fit all parameters
        f = self.partialfunc()
        pinit = np.array([xp_0, yp_0, bmaj_0, bmin_0, pa_0, ampl_0, offset_0])
        psigma = np.array([xp_e, yp_e, bmaj_e, bmin_e, pa_e, ampl_e, offset_e])
        pbounds = (pinit-psigma, pinit+psigma)

        popt, pcov = curve_fit(f, (mg_daz, mg_del), mp_data.flatten(), pinit, bounds=pbounds)
        xp, yp, bmaj, bmin, pa, ampl, offset = popt
        xp_e, yp_e, bmaj_e, bmin_e, pa_e, ampl_e, offset_e = np.sqrt(np.diag(pcov))
        if bmaj < bmin:
            bmaj, bmin = bmin, bmaj
            bmaj_e, bmin_e = bmin_e, bmaj_e
            pa += 90.0

        pa %= 180.0

        mp_fit = f((mg_daz, mg_del), *popt).reshape(mp_data.shape)
        sd = self._estimate_sd(mg_daz, mg_del, mp_data, xp, yp, bmaj, bmin)
        sn = np.abs(ampl/sd)
        chi2 = np.sum(((mp_data-mp_fit)/sd)**2) / mp_data.size

        # step 5: make header
        hd_fit = fits.Header()
        hd_fit['FIT_FUNC'] = 'Gaussian2D', 'fitting function'

        if sn > threshold:
            hd_fit['FIT_STAT'] = 'success', 'fiting status'
            hd_fit['FIT_CHI2'] = chi2, 'reduced Chi^2 (no unit)'
            hd_fit['FIT_SD']   = sd, 'S.D. of map (no unit)'
            hd_fit['FIT_SN']   = sn, 'S/N of amplitude (no unit)'
            hd_fit['PAR_DAZ']  = xp, 'offset in dAz (arcsec)'
            hd_fit['PAR_DEL']  = yp, 'offset in dEl (arcsec)'
            hd_fit['PAR_BMAJ'] = bmaj, 'major beamsize (arcsec)'
            hd_fit['PAR_BMIN'] = bmin, 'minor beamsize (arcsec)'
            hd_fit['PAR_PA']   = pa, 'position angle (degree)'
            hd_fit['PAR_AMPL'] = ampl, 'amplitude (no unit)'
            hd_fit['PAR_OFFS'] = offset, 'offset (no unit)'
            hd_fit['ERR_DAZ']  = xp_e, 'offset in dAz (arcsec)'
            hd_fit['ERR_DEL']  = yp_e, 'offset in dEl (arcsec)'
            hd_fit['ERR_BMAJ'] = bmaj_e, 'major beamsize (arcsec)'
            hd_fit['ERR_BMIN'] = bmin_e, 'minor beamsize (arcsec)'
            hd_fit['ERR_PA']   = pa_e, 'position angle (degree)'
            hd_fit['ERR_AMPL'] = ampl_e, 'amplitude (no unit)'
            hd_fit['ERR_OFFS'] = offset_e, 'offset (no unit)'
        else:
            hd_fit['FIT_STAT'] = 'failed', 'fiting status'

        return hd_fit, mp_fit

    @staticmethod
    def func((x, y), xp, yp, bmaj, bmin, pa, ampl, offset):
        '''Return 2D Gaussian as a flat array.

        Args:
        - (x, y) (array, array) : Input map coordinates [arbitrary unit]
        - xp (float): Peak x (dAz) position of 2D Gaussian [arbitrary unit]
        - yp (float): Peak y (dEl) position of 2D Gaussian [arbitrary unit]
        - bmaj (float): Major beam size of 2D Gaussian [arbitrary unit]
        - bmin (float): Minor beam size of 2D Gaussian [arbitrary unit]
        - pa (float): Position angle of 2D Gaussian [degree]
        - ampl (float): Amplitude of 2D Gaussian [arbitrary unit]
        - offset (float): Baseline offset of 2D Gaussian [arbitrary unit]

        Returns:
        - Gaussian (1D array): Flatten 2D Gaussian [arbitrary unit]
        '''
        xp, yp = float(xp), float(yp)
        bmaj, bmin, pa = float(bmaj), float(bmin), float(pa)
        ampl, offset = float(ampl), float(offset)

        sigx = np.sqrt(2*np.log(2))*bmaj/2
        sigy = np.sqrt(2*np.log(2))*bmin/2
        theta = np.deg2rad(-pa+90.0)

        a = (np.cos(theta)**2)/(2*sigx**2) + (np.sin(theta)**2)/(2*sigy**2)
        b = -(np.sin(2*theta))/(4*sigx**2) + (np.sin(2*theta))/(4*sigy**2)
        c = (np.sin(theta)**2)/(2*sigx**2) + (np.cos(theta)**2)/(2*sigy**2)
        g = offset + ampl * np.exp(-(a*(x-xp)**2-2*b*(x-xp)*(y-yp)+c*(y-yp)**2))

        return g.flatten()

    def partialfunc(self, **kwargs):
        '''Return a new function with some args fixed.

        Args:
        - kwargs: Args and fixed values

        Returns:
        - func (function): A new function with some args fixed
        '''
        args = inspect.getargspec(self.func).args
        args.pop(0) # ('x', 'y')

        args_fnew = list(args)
        for key in kwargs.keys():
            args_fnew.remove(key)

        args_f = []
        for arg in args:
            if arg in kwargs.keys():
                args_f.append('{0}={1}'.format(arg, kwargs[arg]))
            else:
                args_f.append('{0}={1}'.format(arg, arg))

        args_fnew = ','.join(args_fnew)
        args_f = ','.join(args_f)

        func_str = 'lambda (x,y),{0}: self.func((x,y),{1})'
        func = eval(func_str.format(args_fnew, args_f), locals())

        return func

    @staticmethod
    def _estimate_sd(mg_daz, mg_del, mp_data, xp, yp, bmaj, bmin):
        '''Estimate S.D. of data using current fit parameters.

        Args:
        - mg_daz (2D array): meshgrid of dAz [arcsec]
        - mg_del (2D array): meshgrid of dEl [arcsec]
        - mp_data (2D array): data map [arbitrary unit]
        - xp (float): Peak x (dAz) position of 2D Gaussian [arbitrary unit]
        - yp (float): Peak y (dEl) position of 2D Gaussian [arbitrary unit]
        - bmaj (float): Major beam size of 2D Gaussian [arbitrary unit]
        - bmin (float): Minor beam size of 2D Gaussian [arbitrary unit]

        Returns:
        - sd_est (float): Estimated S.D. of data [arbitrary unit]
        '''
        w = bmaj + bmin
        mp_mask = (mg_daz>xp-w) & (mg_daz<xp+w) & (mg_del>yp-w) & (mg_del<yp+w)
        mp_noise = np.ma.array(mp_data, mask=mp_mask)
        sd_est = np.std(mp_noise)

        return sd_est
