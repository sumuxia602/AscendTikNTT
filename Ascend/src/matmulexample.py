import math
from functools import reduce as functools_reduce
import numpy as np
from tbe import tik
import tbe.common.platform as tbe_platform
from tbe.common.utils import para_check
DTYPE_SIZE = {
    'int8': 1,
    'float16': 2,
    'float32': 4,
}

def MK_TO_K1MK0(tik_instance, mk_input_tensor, k1mk0_tensor, dtype, k1, m, k0):
    """change data format mk to k1mk0"""
    src_ub = tik_instance.Tensor(dtype, (k1, m, k0), name="src_ub", scope=tik.scope_ubuf)

    # data_move(m,k) --> (k1,m,k0)
    with tik_instance.for_range(0, k1) as i:
        tik_instance.data_move(src_ub[i * m * k0:], mk_input_tensor[i * k0:], 0, m, k0 * DTYPE_SIZE[dtype] // 32,
                               (k1 - 1) * k0 * DTYPE_SIZE[dtype] // 32, 0)
    # data_move out
    tik_instance.data_move(k1mk0_tensor, src_ub, 0, 1, k1 * m * k0 * DTYPE_SIZE[dtype] // 32, 0, 0)


def KN_TO_K1NK0(tik_instance, kn_input_tensor, k1nk0_tensor, dtype, k1, n, k0):
    """change data format kn to k1nk0"""
    with tik_instance.for_range(0, k1) as index:
        k1nk0_ub = tik_instance.Tensor(dtype, (n, k0), tik.scope_ubuf, "k1nk0_ub")
        src_ub = tik_instance.Tensor(dtype, (k0, n), tik.scope_ubuf, "src_ub")
        burst_len = k0 * n * DTYPE_SIZE[dtype] // 32
        tik_instance.data_move(src_ub, kn_input_tensor[index * k0 * n], 0, 1, burst_len, 0, 0)
        dst_list = [k1nk0_ub[16 * i] for i in range(16)]
        src_list = [src_ub[n * i] for i in range(16)]
        rep_times = n // k0
        dst_rep_stride = k0
        src_rep_stride = 1
        tik_instance.vec_trans_scatter(False, False, dst_list, src_list, rep_times, dst_rep_stride, src_rep_stride)
        tik_instance.data_move(k1nk0_tensor[index * k0 * n], k1nk0_ub, 0, 1, burst_len, 0, 0)

def N1MN0_TO_MN(tik_instance, mn_output_tensor, n1mn0_tensor, dtype, n1, m, n0):
    """change data format n1mn0 to mn"""
    src_ub = tik_instance.Tensor(dtype, (m, n1 * n0), name="src_ub", scope=tik.scope_ubuf)

    # data_move (n1,m,n0) --> (m,n)
    with tik_instance.for_range(0, n1) as i:
        tik_instance.data_move(src_ub[i * n0:], n1mn0_tensor[i * m * n0:], 0, m,
                               n0 * DTYPE_SIZE[dtype] // 32, 0, (n1 - 1) * n0 * DTYPE_SIZE[dtype] // 32)
    # data_move out
    tik_instance.data_move(mn_output_tensor, src_ub, 0, 1, m * n1 * n0 * DTYPE_SIZE[dtype] // 32, 0, 0)


def matmul_tik_compute(params, kernel_name):

    tik_instance = tik.Tik()
    if not isinstance(params, dict):
        params = params.__dict__
    m_size, k_size, n_size = params['M'], params['K'], params['N']
    data_type = params["data_type"]
    m_tiling_size = int(params["m_tiling_size"])
    n_tiling_size = int(params["n_tiling_size"])
    k_tiling_size = int(params['k_tiling_size'])

    m_cycle_times = params["m_cycle_times"]
    n_cycle_times = params["n_cycle_times"]
    k_cycle_times = params["k_cycle_times"]

    # Determine the output type
    if data_type == "float16":
        C_loc_out_type = "float32"
        K0 = 16
    else:
        C_loc_out_type = "int32"
        K0 = 32
    block_size = 16

    n_thread_num = params['n_thread_num']
    m_thread_num = params['m_thread_num']
    k_thread_num = params['k_thread_num']

    mk_gm_input = tik_instance.Tensor(data_type, (m_size, k_size), name="mk_input_gm", scope=tik.scope_gm)
    kn_gm_input = tik_instance.Tensor(data_type, (k_size, n_size), name="kn_input_gm", scope=tik.scope_gm)
    k1mk0_workspace = tik_instance.Tensor(data_type, (k_size // K0, m_size, K0), name="k1mk0_workspace",
                                          scope=tik.scope_gm, is_workspace=True)
    k1nk0_workspace = tik_instance.Tensor(data_type, (k_size // K0, n_size, K0), name="k1nk0_workspace",
                                          scope=tik.scope_gm, is_workspace=True)

    mn_gm_output = tik_instance.Tensor(C_loc_out_type, (m_size, n_size), tik.scope_gm, "mn_output_gm")
    nmk0_workspace = tik_instance.Tensor(C_loc_out_type, (n_size // block_size, m_size, block_size),
                                         name="nmk0_workspace", scope=tik.scope_gm, is_workspace=True)

    MK_TO_K1MK0(tik_instance, mk_gm_input, k1mk0_workspace, data_type, k_size // K0, m_size, K0)
    KN_TO_K1NK0(tik_instance, kn_gm_input, k1nk0_workspace, data_type, k_size // K0, n_size, K0)

    # Tiling is realized through the for_range() loop.
    with tik_instance.for_range(0, 2, block_num=1) as core_id:
        with tik_instance.for_range(0, n_cycle_times // 2, thread_num=n_thread_num) as n_idx:
            with tik_instance.for_range(0, m_cycle_times, thread_num=m_thread_num) as m_idx:
                dst_l0c = tik_instance.Tensor(C_loc_out_type, [n_tiling_size // 16, m_tiling_size, 16], name='dst_l0c',
                                              scope=tik.scope_cbuf_out)
                with tik_instance.for_range(0, k_cycle_times,
                                            thread_num=k_thread_num) as k_idx:
                    # Calculation result data transfer.
                    inputa_l1 = tik_instance.Tensor(params['data_type'], [k_tiling_size // K0, m_tiling_size, K0],
                                                    name="A_tiling_l1", scope=tik.scope_cbuf)
                    tik_instance.data_move(inputa_l1,
                                           k1mk0_workspace[k_idx * k_tiling_size // K0, m_idx * m_tiling_size, :],
                                           0, k_tiling_size // K0, m_tiling_size, m_size - m_tiling_size, 0)
                    inputb_l1 = tik_instance.Tensor(params["data_type"], [k_tiling_size // K0, n_tiling_size, K0],
                                                    name="B_tiling_l1", scope=tik.scope_cbuf)
                    if n_size - n_tiling_size > 65535:
                        with tik_instance.for_range(0, k_tiling_size // K0) \
                                as dma_k_idx:
                            tik_instance.data_move(inputb_l1[dma_k_idx, :, :],
                                                   k1nk0_workspace[k_idx * k_tiling_size // K0 + dma_k_idx,
                                                   (core_id * n_cycle_times // 2 + n_idx) * n_tiling_size, :],
                                                   0, 1, n_tiling_size, 0, 0)
                    else:
                        tik_instance.data_move(inputb_l1, k1nk0_workspace[k_idx * k_tiling_size // K0,
                                                          (core_id * n_cycle_times // 2 + n_idx) * n_tiling_size, :], 0,
                                               k_tiling_size // K0, n_tiling_size, n_size - n_tiling_size, 0)
                    # Call matmul API to matrix multiplication calculation.
                    with tik_instance.if_scope(k_idx == 0):
                        tik_instance.matmul(dst_l0c, inputa_l1, inputb_l1, m_tiling_size, k_tiling_size, n_tiling_size,
                                            init_l1out=True)
                    with tik_instance.else_scope():
                        tik_instance.matmul(dst_l0c, inputa_l1, inputb_l1, m_tiling_size, k_tiling_size, n_tiling_size,
                                            init_l1out=False)
                tik_instance.fixpipe(nmk0_workspace[n_tiling_size // 16 * (core_id * n_cycle_times // 2 + n_idx),
                                     m_idx * m_tiling_size, :], dst_l0c, n_tiling_size // 16, m_tiling_size * 16 *
                                     DTYPE_SIZE[C_loc_out_type] // 32,
                                     (m_size - m_tiling_size) * 16 * DTYPE_SIZE[C_loc_out_type] // 32, 0)

    N1MN0_TO_MN(tik_instance, mn_gm_output, nmk0_workspace, C_loc_out_type, n_size // K0, m_size, K0)

    tik_instance.BuildCCE(kernel_name=kernel_name,
                          inputs=[mk_gm_input, kn_gm_input], outputs=[mn_gm_output])
    return tik_instance


if __name__ == "__main__":
    shape_a = [16, 64]
    shape_b = [64, 1024]
    # 输入参数和tiling信息
    params = {
        'M': shape_a[0],
        'K': shape_a[1],
        'N': shape_b[1],
        'data_type': "float16",
        'm_tiling_size': 16,
        'm_cycle_times': 1,
        'm_thread_num': 1,
        'n_tiling_size': 64,
        'n_cycle_times': 16,
        'n_thread_num': 1,
        'k_tiling_size': 32,
        'k_cycle_times': 2,
        'k_thread_num': 2,
    }
    tik_instance = matmul_tik_compute(params, "simple_matmul")


