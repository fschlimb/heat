"""
Collection of solvers for systems of linear equations.
"""
import heat as ht
from ..dndarray import DNDarray
from typing import List, Dict, Any, TypeVar, Union, Tuple, Optional

import torch

__all__ = ["cg", "lanczos"]


def cg(A: DNDarray, b: DNDarray, x0: DNDarray, out: Optional[DNDarray] = None) -> DNDarray:
    """
    Conjugate gradients method for solving a system of linear equations :math: `Ax = b`

    Parameters
    ----------
    A : DNDarray
        2D symmetric, positive definite Matrix
    b : DNDarray
        1D vector
    x0 : DNDarray
        Arbitrary 1D starting vector
    out : DNDarray, optional
        Output Vector
    """
    if not isinstance(A, DNDarray) or not isinstance(b, DNDarray) or not isinstance(x0, DNDarray):
        raise TypeError(
            "A, b and x0 need to be of type ht.DNDarray, but were {}, {}, {}".format(
                type(A), type(b), type(x0)
            )
        )

    if not A.ndim == 2:
        raise RuntimeError("A needs to be a 2D matrix")
    if not b.ndim == 1:
        raise RuntimeError("b needs to be a 1D vector")
    if not x0.ndim == 1:
        raise RuntimeError("c needs to be a 1D vector")

    r = b - ht.matmul(A, x0)
    p = r
    rsold = ht.matmul(r, r)
    x = x0

    for i in range(len(b)):
        Ap = ht.matmul(A, p)
        alpha = rsold / ht.matmul(p, Ap)
        x = x + alpha * p
        r = r - alpha * Ap
        rsnew = ht.matmul(r, r)
        if ht.sqrt(rsnew).item() < 1e-10:
            print("Residual reaches tolerance in it = {}".format(i))
            if out is not None:
                out = x
                return out
            return x
        p = r + ((rsnew / rsold) * p)
        rsold = rsnew

    if out is not None:
        out = x
        return out
    return x


def lanczos(
    A: DNDarray,
    m: int,
    v0: Optional[DNDarray] = None,
    V_out: Optional[DNDarray] = None,
    T_out: Optional[DNDarray] = None,
) -> Tuple[DNDarray, DNDarray]:
    r"""
    The Lanczos algorithm is an iterative approximation of the solution to the eigenvalue problem, as an adaptation of
    power methods to find the m "most useful" (tending towards extreme highest/lowest) eigenvalues and eigenvectors of
    an :math:`n \times n` Hermitian matrix, where often :math:`m<<n`.
    It returns two matrices :math:`V` and :math:`T`, where:

        - :math:`V` is a Matrix of size :math:`n\times m`, with orthonormal columns, that span the Krylow subspace \n
        - :math:`T` is a Tridiagonal matrix of size :math:`m\times m`, with coefficients :math:`\alpha_1,..., \alpha_n`
          on the diagonal and coefficients :math:`\beta_1,...,\beta_{n-1}` on the side-diagonals\n

    Parameters
    ----------
    A : DNDarray
        2D symmetric, positive definite Matrix
    m : int
        Number of Lanczos iterations
    v0 : DNDarray, optional
        1D starting vector of Euclidian norm 1. If not provided, a random vector will be used to start the algorithm
    V_out : DNDarray, optional
        Output Matrix for the Krylow vectors, Shape = (n, m)
    T_out : DNDarray, optional
        Output Matrix for the Tridiagonal matrix, Shape = (m, m)
    """
    if not isinstance(A, DNDarray):
        raise TypeError("A needs to be of type ht.dndarra, but was {}".format(type(A)))

    if not (A.ndim == 2):
        raise RuntimeError("A needs to be a 2D matrix")
    if not isinstance(m, (int, float)):
        raise TypeError("m must be eiter int or float, but was {}".format(type(m)))

    n, column = A.shape
    if n != column:
        raise TypeError("Input Matrix A needs to be symmetric.")
    T = ht.zeros((m, m))
    if A.split == 0:
        # This is done for better memory access in the reorthogonalization Gram-Schmidt algorithm
        V = ht.ones((n, m), split=0, dtype=A.dtype, device=A.device)
    else:
        V = ht.ones((n, m), split=None, dtype=A.dtype, device=A.device)

    if v0 is None:
        vr = ht.random.rand(n, split=V.split)
        v0 = vr / ht.norm(vr)
    else:
        if v0.split != V.split:
            v0.resplit_(axis=V.split)
    # # 0th iteration
    # # vector v0 has euklidian norm = 1
    w = ht.matmul(A, v0)
    alpha = ht.dot(w, v0)
    w = w - alpha * v0
    T[0, 0] = alpha
    V[:, 0] = v0
    for i in range(1, int(m)):
        beta = ht.norm(w)
        if abs(beta) < 1e-10:
            # print("Lanczos breakdown in iteration {}".format(i))
            # Lanczos Breakdown, pick a random vector to continue
            vr = ht.random.rand(n, dtype=A.dtype, split=V.split)
            # orthogonalize v_r with respect to all vectors v[i]
            for j in range(i):
                vi_loc = V.larray[:, j]
                a = torch.dot(vr.larray, vi_loc)
                b = torch.dot(vi_loc, vi_loc)
                A.comm.Allreduce(ht.communication.MPI.IN_PLACE, a, ht.communication.MPI.SUM)
                A.comm.Allreduce(ht.communication.MPI.IN_PLACE, b, ht.communication.MPI.SUM)
                vr.larray = vr.larray - a / b * vi_loc
            # normalize v_r to Euclidian norm 1 and set as ith vector v
            vi = vr / ht.norm(vr)
        else:
            vr = w

            # Reorthogonalization
            # ToDo: Rethink this; mask torch calls, See issue #494
            # This is the fast solution, using item access on the ht.dndarray level is way slower
            for j in range(i):
                vi_loc = V.larray[:, j]
                a = torch.dot(vr._DNDarray__array, vi_loc)
                b = torch.dot(vi_loc, vi_loc)
                A.comm.Allreduce(ht.communication.MPI.IN_PLACE, a, ht.communication.MPI.SUM)
                A.comm.Allreduce(ht.communication.MPI.IN_PLACE, b, ht.communication.MPI.SUM)
                vr._DNDarray__array = vr._DNDarray__array - a / b * vi_loc

            vi = vr / ht.norm(vr)

        w = ht.matmul(A, vi)
        alpha = ht.dot(w, vi)

        w = w - alpha * vi - beta * V[:, i - 1]

        T[i - 1, i] = beta
        T[i, i - 1] = beta
        T[i, i] = alpha
        V[:, i] = vi

    if V.split is not None:
        V.resplit_(axis=None)

    if T_out is not None:
        T_out = T.copy()
        if V_out is not None:
            V_out = V.copy()
            return V_out, T_out
        return V, T_out
    elif V_out is not None:
        V_out = V.copy()
        return V_out, T

    return V, T
