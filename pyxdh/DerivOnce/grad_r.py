# basic utilities
import numpy as np
from opt_einsum import contract as einsum
# pyscf utilities
from pyscf import grad
from pyscf.scf import _vhf
# pyxdh utilities
from pyxdh.DerivOnce import DerivOnceSCF, DerivOnceNCDFT, DerivOnceMP2, DerivOnceXDH
from pyxdh.Utilities import GridIterator, KernelHelper, timing, cached_property
# pytest
from pyscf import gto, scf, dft, mp
from pkg_resources import resource_filename
from pyxdh.Utilities import FormchkInterface
import pickle


# Cubic Inheritance: A2
class GradSCF(DerivOnceSCF):

    def Ax1_Core(self, si, sj, sk, sl, reshape=True):

        C, Co = self.C, self.Co
        natm, nao = self.natm, self.nao
        mol = self.mol
        cx = self.cx
        so = self.so

        dmU = C @ self.U_1[:, :, so] @ Co.T
        dmU += dmU.swapaxes(-1, -2)
        dmU.shape = (natm, 3, nao, nao)

        sij_none = si is None and sj is None
        skl_none = sk is None and sl is None

        @timing
        def fx(X_):
            if not isinstance(X_, np.ndarray):
                return 0
            X = X_.copy()  # type: np.ndarray
            shape1 = list(X.shape)
            X.shape = (-1, shape1[-2], shape1[-1])
            if skl_none:
                dmX = X
                if dmX.shape[-2] != nao or dmX.shape[-1] != nao:
                    raise ValueError("if `sk`, `sl` is None, we assume that mo1 passed in is an AO-based matrix!")
            else:
                dmX = C[:, sk] @ X @ C[:, sl].T
            dmX += dmX.transpose((0, 2, 1))

            ax_ao = np.empty((natm, 3, dmX.shape[0], nao, nao))

            # Actual calculation
            # (ut v | k l), (ut k | v l)
            j_1, k_1 = _vhf.direct_mapdm(
                mol._add_suffix('int2e_ip1'), "s2kl",
                ("lk->s1ij", "jk->s1il"),
                dmX, 3,
                mol._atm, mol._bas, mol._env
            )
            if dmX.shape[0] == 1:  # dm shape is 1 * nao * nao, then j_1, k_1 do not retain dimension of dm.shape[0]
                j_1, k_1 = j_1[None, :], k_1[None, :]
            j_1, k_1 = j_1.swapaxes(0, 1), k_1.swapaxes(0, 1)

            # HF Part
            for A in range(natm):
                ax = np.zeros((3, dmX.shape[0], nao, nao))
                shl0, shl1, p0, p1 = mol.aoslice_by_atom()[A]
                sA = slice(p0, p1)  # equivalent to mol_slice(A)
                ax[:, :, sA, :] -= 2 * j_1[:, :, sA, :]
                ax[:, :, :, sA] -= 2 * j_1[:, :, sA, :].swapaxes(-1, -2)
                ax[:, :, sA, :] += cx * k_1[:, :, sA, :]
                ax[:, :, :, sA] += cx * k_1[:, :, sA, :].swapaxes(-1, -2)
                # (kt l | u v), (kt u | l v)
                j_1A, k_1A = _vhf.direct_mapdm(
                    mol._add_suffix('int2e_ip1'), "s2kl",
                    ("ji->s1kl", "li->s1kj"),
                    dmX[:, :, p0:p1], 3,
                    mol._atm, mol._bas, mol._env,
                    shls_slice=((shl0, shl1) + (0, mol.nbas) * 3)
                )
                if dmX.shape[0] == 1:  # dm shape is 1 * nao * nao, then j_1A, k_1A do not retain dimension of dm.shape[0]
                    j_1A, k_1A = j_1A[None, :], k_1A[None, :]
                j_1A, k_1A = j_1A.swapaxes(0, 1), k_1A.swapaxes(0, 1)
                ax -= 4 * j_1A
                ax += cx * (k_1A + k_1A.swapaxes(-1, -2))

                ax_ao[A] = ax

            # GGA Part
            if self.xc_type == "GGA":
                grdit = GridIterator(self.mol, self.grids, self.D, deriv=3, memory=self.grdit_memory)
                for grdh in grdit:
                    kerh = KernelHelper(grdh, self.xc, deriv=3)
                    # Define some kernel and density derivative alias
                    pd_frr = kerh.frrr * grdh.A_rho_1 + kerh.frrg * grdh.A_gamma_1
                    pd_frg = kerh.frrg * grdh.A_rho_1 + kerh.frgg * grdh.A_gamma_1
                    pd_fgg = kerh.frgg * grdh.A_rho_1 + kerh.fggg * grdh.A_gamma_1
                    pd_fg = kerh.frg * grdh.A_rho_1 + kerh.fgg * grdh.A_gamma_1
                    pd_rho_1 = grdh.A_rho_2

                    # Form dmX density grid
                    rho_X_0 = np.array([grdh.get_rho_0(dm) for dm in dmX])
                    rho_X_1 = np.array([grdh.get_rho_1(dm) for dm in dmX])
                    pd_rho_X_0 = np.array([grdh.get_A_rho_1(dm) for dm in dmX]).transpose((1, 2, 0, 3))
                    pd_rho_X_1 = np.array([grdh.get_A_rho_2(dm) for dm in dmX]).transpose((1, 2, 0, 3, 4))

                    # Define temporary intermediates
                    tmp_M_0 = (
                            + einsum("g, Bg -> Bg", kerh.frr, rho_X_0)
                            + 2 * einsum("g, wg, Bwg -> Bg", kerh.frg, grdh.rho_1, rho_X_1)
                    )
                    tmp_M_1 = (
                            + 4 * einsum("g, Bg, rg -> Brg", kerh.frg, rho_X_0, grdh.rho_1)
                            + 8 * einsum("g, wg, Bwg, rg -> Brg", kerh.fgg, grdh.rho_1, rho_X_1, grdh.rho_1)
                            + 4 * einsum("g, Brg -> Brg", kerh.fg, rho_X_1)
                    )
                    pd_tmp_M_0 = (
                            + einsum("Atg, Bg -> AtBg", pd_frr, rho_X_0)
                            + einsum("g, AtBg -> AtBg", kerh.frr, pd_rho_X_0)
                            + 2 * einsum("Atg, wg, Bwg -> AtBg", pd_frg, grdh.rho_1, rho_X_1)
                            + 2 * einsum("g, Atwg, Bwg -> AtBg", kerh.frg, pd_rho_1, rho_X_1)
                            + 2 * einsum("g, wg, AtBwg -> AtBg", kerh.frg, grdh.rho_1, pd_rho_X_1)
                    )
                    pd_tmp_M_1 = (
                            + 4 * einsum("Atg, Bg, rg -> AtBrg", pd_frg, rho_X_0, grdh.rho_1)
                            + 4 * einsum("g, Bg, Atrg -> AtBrg", kerh.frg, rho_X_0, pd_rho_1)
                            + 4 * einsum("g, AtBg, rg -> AtBrg", kerh.frg, pd_rho_X_0, grdh.rho_1)
                            + 8 * einsum("Atg, wg, Bwg, rg -> AtBrg", pd_fgg, grdh.rho_1, rho_X_1, grdh.rho_1)
                            + 8 * einsum("g, Atwg, Bwg, rg -> AtBrg", kerh.fgg, pd_rho_1, rho_X_1, grdh.rho_1)
                            + 8 * einsum("g, wg, Bwg, Atrg -> AtBrg", kerh.fgg, grdh.rho_1, rho_X_1, pd_rho_1)
                            + 8 * einsum("g, wg, AtBwg, rg -> AtBrg", kerh.fgg, grdh.rho_1, pd_rho_X_1, grdh.rho_1)
                            + 4 * einsum("Atg, Brg -> AtBrg", pd_fg, rho_X_1)
                            + 4 * einsum("g, AtBrg -> AtBrg", kerh.fg, pd_rho_X_1)
                    )

                    contrib1 = np.zeros((natm, 3, dmX.shape[0], nao, nao))
                    contrib1 += einsum("AtBg, gu, gv -> AtBuv", pd_tmp_M_0, grdh.ao_0, grdh.ao_0)
                    contrib1 += einsum("AtBrg, rgu, gv -> AtBuv", pd_tmp_M_1, grdh.ao_1, grdh.ao_0)
                    contrib1 += contrib1.swapaxes(-1, -2)

                    tmp_contrib = (
                            - 2 * einsum("Bg, tgu, gv -> tBuv", tmp_M_0, grdh.ao_1, grdh.ao_0)
                            - einsum("Brg, trgu, gv -> tBuv", tmp_M_1, grdh.ao_2, grdh.ao_0)
                            - einsum("Brg, tgu, rgv -> tBuv", tmp_M_1, grdh.ao_1, grdh.ao_1)
                    )

                    contrib2 = np.zeros((natm, 3, dmX.shape[0], nao, nao))
                    for A in range(natm):
                        sA = self.mol_slice(A)
                        contrib2[A, :, :, sA] += tmp_contrib[:, :, sA]

                    contrib2 += contrib2.swapaxes(-1, -2)

                    # U contribution to \partial_{A_t} A
                    rho_U_0 = einsum("Atuv, gu, gv -> Atg", dmU, grdh.ao_0, grdh.ao_0)
                    rho_U_1 = 2 * einsum("Atuv, rgu, gv -> Atrg", dmU, grdh.ao_1, grdh.ao_0)
                    gamma_U_0 = 2 * einsum("rg, Atrg -> Atg", grdh.rho_1, rho_U_1)
                    pdU_frr = kerh.frrr * rho_U_0 + kerh.frrg * gamma_U_0
                    pdU_frg = kerh.frrg * rho_U_0 + kerh.frgg * gamma_U_0
                    pdU_fgg = kerh.frgg * rho_U_0 + kerh.fggg * gamma_U_0
                    pdU_fg = kerh.frg * rho_U_0 + kerh.fgg * gamma_U_0
                    pdU_rho_1 = rho_U_1
                    pdU_tmp_M_0 = (
                            + einsum("Atg, Bg -> AtBg", pdU_frr, rho_X_0)
                            + 2 * einsum("Atg, wg, Bwg -> AtBg", pdU_frg, grdh.rho_1, rho_X_1)
                            + 2 * einsum("g, Atwg, Bwg -> AtBg", kerh.frg, pdU_rho_1, rho_X_1)
                    )
                    pdU_tmp_M_1 = (
                            + 4 * einsum("Atg, Bg, rg -> AtBrg", pdU_frg, rho_X_0, grdh.rho_1)
                            + 4 * einsum("g, Bg, Atrg -> AtBrg", kerh.frg, rho_X_0, pdU_rho_1)
                            + 8 * einsum("Atg, wg, Bwg, rg -> AtBrg", pdU_fgg, grdh.rho_1, rho_X_1, grdh.rho_1)
                            + 8 * einsum("g, Atwg, Bwg, rg -> AtBrg", kerh.fgg, pdU_rho_1, rho_X_1, grdh.rho_1)
                            + 8 * einsum("g, wg, Bwg, Atrg -> AtBrg", kerh.fgg, grdh.rho_1, rho_X_1, pdU_rho_1)
                            + 4 * einsum("Atg, Brg -> AtBrg", pdU_fg, rho_X_1)
                    )

                    contrib3 = np.zeros((natm, 3, dmX.shape[0], nao, nao))
                    contrib3 += einsum("AtBg, gu, gv -> AtBuv", pdU_tmp_M_0, grdh.ao_0, grdh.ao_0)
                    contrib3 += einsum("AtBrg, rgu, gv -> AtBuv", pdU_tmp_M_1, grdh.ao_1, grdh.ao_0)
                    contrib3 += contrib3.swapaxes(-1, -2)

                    ax_ao += contrib1 + contrib2 + contrib3

            ax_ao.shape = (natm * 3, dmX.shape[0], nao, nao)

            if not sij_none:
                ax_ao = einsum("ABuv, ui, vj -> ABij", ax_ao, C[:, si], C[:, sj])
            if reshape:
                shape1.pop()
                shape1.pop()
                shape1.insert(0, ax_ao.shape[0])
                shape1.append(ax_ao.shape[-2])
                shape1.append(ax_ao.shape[-1])
                ax_ao.shape = shape1

            return ax_ao

        return fx

    @cached_property
    def H_1_ao(self):
        return np.array([self.scf_grad.hcore_generator()(A) for A in range(self.natm)])\
            .reshape((-1, self.nao, self.nao))

    @cached_property
    def F_1_ao(self):
        return np.array(self.scf_hess.make_h1(self.C, self.mo_occ)).reshape((-1, self.nao, self.nao))

    @cached_property
    def S_1_ao(self):
        int1e_ipovlp = self.mol.intor("int1e_ipovlp")

        def get_S_S_ao(A):
            ao_matrix = np.zeros((3, self.nao, self.nao))
            sA = self.mol_slice(A)
            ao_matrix[:, sA] = -int1e_ipovlp[:, sA]
            return ao_matrix + ao_matrix.swapaxes(1, 2)

        S_1_ao = np.array([get_S_S_ao(A) for A in range(self.natm)]).reshape((-1, self.nao, self.nao))
        return S_1_ao

    @cached_property
    def eri1_ao(self):
        nao = self.nao
        natm = self.natm
        int2e_ip1 = self.mol.intor("int2e_ip1")
        eri1_ao = np.zeros((natm, 3, nao, nao, nao, nao))
        for A in range(natm):
            sA = self.mol_slice(A)
            eri1_ao[A, :, sA, :, :, :] -= int2e_ip1[:, sA]
            eri1_ao[A, :, :, sA, :, :] -= int2e_ip1[:, sA].transpose(0, 2, 1, 3, 4)
            eri1_ao[A, :, :, :, sA, :] -= int2e_ip1[:, sA].transpose(0, 3, 4, 1, 2)
            eri1_ao[A, :, :, :, :, sA] -= int2e_ip1[:, sA].transpose(0, 3, 4, 2, 1)
        return eri1_ao.reshape((-1, self.nao, self.nao, self.nao, self.nao))

    def _get_E_1(self):
        cx, xc = self.cx, self.xc
        so = self.so
        mol, natm = self.mol, self.natm
        D = self.D
        H_1_ao = self.H_1_ao
        S_1_mo = self.S_1_mo
        F_0_mo = self.F_0_mo
        grids = self.grids
        grdit_memory = self.grdit_memory
        scf_grad = self.scf_grad

        grad_total = np.zeros(natm * 3)

        # From memory consumption point, we use higher subroutines in PySCF to generate ERI contribution
        jk_1 = (
            + 2 * scf_grad.get_j(dm=D)
            - cx * scf_grad.get_k(dm=D)
        )
        for A in range(natm):
            sA = self.mol_slice(A)
            grad_total[3 * A: 3 * (A + 1)] += einsum("tuv, uv -> t", jk_1[:, sA], D[sA])

        grad_total += einsum("Auv, uv -> A", H_1_ao, D)
        grad_total -= 2 * einsum("Aij, ij -> A", S_1_mo[:, so, so], F_0_mo[so, so])
        grad_total += grad.rhf.grad_nuc(mol).reshape(-1)

        # GGA part contiribution
        if self.xc_type == "GGA":
            grdit = GridIterator(mol, grids, D, deriv=2, memory=grdit_memory)
            for grdh in grdit:
                kerh = KernelHelper(grdh, xc)
                grad_total += (
                    + einsum("g, Atg -> At", kerh.fr, grdh.A_rho_1)
                    + 2 * einsum("g, rg, Atrg -> At", kerh.fg, grdh.rho_1, grdh.A_rho_2)
                ).reshape(-1)

        return grad_total.reshape(natm, 3)


# Cubic Inheritance: B2
class GradNCDFT(DerivOnceNCDFT, GradSCF):

    @property
    def DerivOnceMethod(self):
        return GradSCF

    def _get_E_1(self):
        natm = self.natm
        so, sv = self.so, self.sv
        B_1 = self.B_1
        Z = self.Z
        E_1 = 4 * einsum("ai, Aai -> A", Z, B_1[:, sv, so]).reshape((natm, 3))
        E_1 += self.nc_deriv.E_1
        return E_1


# Cubic Inheritance: C2
class GradMP2(DerivOnceMP2, GradSCF):

    def _get_E_1(self):
        so, sv = self.so, self.sv
        natm = self.natm
        E_1 = (
            + np.einsum("pq, Apq -> A", self.D_r, self.B_1)
            + np.einsum("pq, Apq -> A", self.W_I, self.S_1_mo)
            + 2 * np.einsum("iajb, Aiajb -> A", self.T_iajb, self.eri1_mo[:, so, sv, so, sv])
        ).reshape(natm, 3)
        E_1 += super(GradMP2, self)._get_E_1()
        return E_1


# Cubic Inheritance: D2
class GradXDH(DerivOnceXDH, GradMP2, GradNCDFT):

    def _get_E_1(self):
        so, sv = self.so, self.sv
        natm = self.natm
        E_1 = (
            + np.einsum("pq, Apq -> A", self.D_r, self.B_1)
            + np.einsum("pq, Apq -> A", self.W_I, self.S_1_mo)
            + 2 * np.einsum("iajb, Aiajb -> A", self.T_iajb, self.eri1_mo[:, so, sv, so, sv])
        ).reshape(natm, 3)
        E_1 += self.nc_deriv.E_1
        return E_1


class TestGradR:

    mol = gto.Mole(atom="N 0. 0. 0.; H 1.5 0. 0.2; H 0.1 1.2 0.; H 0. 0. 1.", basis="6-31G", verbose=0).build()
    grids = dft.Grids(mol)
    grids.atom_grid = (99, 590)
    grids.build()
    grids_cphf = dft.Grids(mol)
    grids_cphf.atom_grid = (50, 194)
    grids_cphf.build()

    def test_r_rhf_grad(self):
        scf_eng = scf.RHF(self.mol).run()
        scf_grad = scf_eng.Gradients().run()
        gradh = GradSCF({"scf_eng": scf_eng})
        formchk = FormchkInterface(resource_filename("pyxdh", "Validation/gaussian/NH3-HF-freq.fchk"))
        # ASSERT: energy - Gaussian
        assert np.allclose(gradh.eng, formchk.total_energy())
        # ASSERT: energy - PySCF
        assert np.allclose(gradh.eng, scf_eng.e_tot)
        # ASSERT: grad - Gaussian
        assert np.allclose(gradh.E_1, formchk.grad(), atol=1e-6, rtol=1e-4)
        # ASSERT: grad - PySCF
        assert np.allclose(gradh.E_1, scf_grad.de, atol=1e-6, rtol=1e-4)

    def test_r_b3lyp_grad(self):
        scf_eng = dft.RKS(self.mol, xc="B3LYPg"); scf_eng.grids = self.grids; scf_eng.run()
        scf_grad = scf_eng.Gradients().run()
        gradh = GradSCF({"scf_eng": scf_eng})
        formchk = FormchkInterface(resource_filename("pyxdh", "Validation/gaussian/NH3-B3LYP-freq.fchk"))
        # ASSERT: energy - Gaussian
        assert np.allclose(gradh.eng, formchk.total_energy())
        # ASSERT: energy - PySCF
        assert np.allclose(gradh.eng, scf_eng.e_tot)
        # ASSERT: grad - Gaussian
        assert np.allclose(gradh.E_1, formchk.grad(), atol=5e-6, rtol=1e-4)
        # ASSERT: grad - PySCF
        assert np.allclose(gradh.E_1, scf_grad.de, atol=1e-6, rtol=1e-4)

    def test_r_hfb3lyp_grad(self):
        scf_eng = scf.RHF(self.mol).run()
        nc_eng = dft.RKS(self.mol, xc="B3LYPg")
        nc_eng.grids = self.grids
        gradh = GradNCDFT({"scf_eng": scf_eng, "nc_eng": nc_eng})
        with open(resource_filename("pyxdh", "Validation/numerical_deriv/NH3-HFB3LYP-grad.dat"), "rb") as f:
            ref_grad = pickle.load(f).reshape(-1, 3)
        # ASSERT: energy - theoretical
        assert np.allclose(gradh.eng, nc_eng.energy_tot(dm=scf_eng.make_rdm1()))
        # ASSERT: grad - numerical
        assert np.allclose(gradh.E_1, ref_grad, atol=1e-6, rtol=1e-4)

    def test_r_mp2_grad(self):
        scf_eng = scf.RHF(self.mol).run()
        mp2_eng = mp.MP2(scf_eng).run()
        mp2_grad = mp2_eng.Gradients().run()
        gradh = GradMP2({"scf_eng": scf_eng})
        formchk = FormchkInterface(resource_filename("pyxdh", "Validation/gaussian/NH3-MP2-freq.fchk"))
        # ASSERT: energy - Gaussian
        assert np.allclose(gradh.eng, formchk.total_energy())
        # ASSERT: energy - PySCF
        assert np.allclose(gradh.eng, mp2_eng.e_tot)
        # ASSERT: grad - Gaussian
        assert np.allclose(gradh.E_1, formchk.grad(), atol=1e-6, rtol=1e-4)
        # ASSERT: grad - PySCF
        assert np.allclose(gradh.E_1, mp2_grad.de, atol=1e-6, rtol=1e-4)

    def test_r_b2plyp_grad(self):
        scf_eng = dft.RKS(self.mol, xc="0.53*HF + 0.47*B88, 0.73*LYP"); scf_eng.grids = self.grids; scf_eng.run()
        gradh = GradMP2({"scf_eng": scf_eng, "cc": 0.27, "cphf_grids": self.grids_cphf})
        formchk = FormchkInterface(resource_filename("pyxdh", "Validation/gaussian/NH3-B2PLYP-freq.fchk"))
        # ASSERT: energy - Gaussian
        assert np.allclose(gradh.eng, formchk.total_energy())
        # ASSERT: grad - Gaussian
        assert np.allclose(gradh.E_1, formchk.grad(), atol=1e-6, rtol=1e-4)

    def test_r_xyg3_grad(self):
        scf_eng = dft.RKS(self.mol, xc="B3LYPg"); scf_eng.grids = self.grids; scf_eng.run()
        nc_eng = dft.RKS(self.mol, xc="0.8033*HF - 0.0140*LDA + 0.2107*B88, 0.6789*LYP"); nc_eng.grids = self.grids
        config = {"scf_eng": scf_eng, "nc_eng": nc_eng, "cc": 0.3211, "cphf_grids": self.grids_cphf}
        gradh = GradXDH(config)
        formchk = FormchkInterface(resource_filename("pyxdh", "Validation/gaussian/NH3-XYG3-freq.fchk"))
        # ASSERT: energy - Gaussian
        assert np.allclose(gradh.eng, formchk.total_energy())
        # ASSERT: grad - Gaussian
        assert np.allclose(gradh.E_1, formchk.grad(), atol=5e-6, rtol=1e-4)

    def test_r_xygjos_grad(self):
        scf_eng = dft.RKS(self.mol, xc="B3LYPg"); scf_eng.grids = self.grids; scf_eng.run()
        nc_eng = dft.RKS(self.mol, xc="0.7731*HF + 0.2269*LDA, 0.2309*VWN3 + 0.2754*LYP"); nc_eng.grids = self.grids
        config = {"scf_eng": scf_eng, "nc_eng": nc_eng, "cc": 0.4364, "ss": 0., "cphf_grids": self.grids_cphf}
        gradh = GradXDH(config)
        formchk = FormchkInterface(resource_filename("pyxdh", "Validation/gaussian/NH3-XYGJOS-freq.fchk"))
        # ASSERT: energy - Gaussian
        assert np.allclose(gradh.eng, formchk.total_energy())
        # ASSERT: grad - Gaussian
        assert np.allclose(gradh.E_1, formchk.grad(), atol=5e-6, rtol=1e-4)