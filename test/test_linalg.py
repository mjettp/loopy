from __future__ import division

import numpy as np
import numpy.linalg as la
import pyopencl as cl
import pyopencl.array as cl_array
import pyopencl.clrandom as cl_random
import loopy as lp

from pyopencl.tools import pytest_generate_tests_for_pyopencl \
        as pytest_generate_tests




def make_well_conditioned_dev_matrix(queue, shape, dtype=np.float32, 
        order="C", ran_factor=1, id_factor=5, inc_factor=0, od=0):
    if isinstance(shape, int):
        shape = (shape, shape)
    l = max(shape)
    eye_ish = id_factor*np.eye(l, k=od)
    if inc_factor:
        eye_ish[np.arange(l), np.arange(l)] = inc_factor*np.arange(l)
    ary = np.asarray(
        ran_factor*np.random.randn(*shape)
        + eye_ish[:shape[0], :shape[1]],
        dtype=dtype, order=order)

    return cl_array.to_device(queue, ary)




DO_CHECK = True

DEBUG_PREAMBLE = r"""
    #pragma OPENCL EXTENSION cl_amd_printf: enable
    #define MY_J (j_outer*64+j_inner_outer*16+j_inner_inner)
    #define MY_I (i_outer*16+i_inner)
    #define IFDIAG if (MY_I == MY_J)
    #define TST(S) if (MY_J == 144 && MY_I == 16-48) \
            for (int aa = 0; aa < 16: ++ab) \
                for (int bb = 0; bb < 16: ++bb) 
    """




def check_error(refsol, sol):
    if not DO_CHECK:
        return

    if sol.shape == 2:
        norm_order = "fro"
    else:
        norm_order = 2

    rel_err = la.norm(refsol-sol, norm_order)/la.norm(refsol, norm_order)
    if rel_err > 1e-5 or np.isinf(rel_err) or np.isnan(rel_err):
        if 1:
            import matplotlib.pyplot as pt
            pt.imshow(refsol-sol, interpolation="nearest")
            pt.colorbar()
            pt.show()
        elif 0:
            print "---------------------------"
            print "ACTUAL"
            print "---------------------------"
            np.set_printoptions(threshold=1000000, linewidth=200)
            print sol[:16,:16]
            print "---------------------------"
            print "CORRECT"
            print "---------------------------"
            print refsol[:16,:16]
        raise RuntimeError("check failed, rel err=%g" % rel_err)




def get_suitable_size(ctx):
    dev, = ctx.devices
    if dev.type == cl.device_type.CPU:
        return 160
    else:
        return 1600




def check_float4(result, ref_result):
    for comp in ["x", "y", "z", "w"]:
        return np.allclose(ref_result[comp], result[comp], rtol=1e-3, atol=1e-3), None

def test_axpy(ctx_factory):
    ctx = ctx_factory()

    n = 3145182

    vec = cl_array.vec

    for dtype, check, a, b in [
            (np.complex64, None, 5, 7),
            (vec.float4, check_float4,
                vec.make_float4(1, 2, 3, 4), vec.make_float4(6, 7, 8, 9)),
            (np.float32, None, 5, 7),
            ]:
        knl = lp.make_kernel(ctx.devices[0],
                "[n] -> {[i]: 0<=i<n}",
                [
                    "z[i] = a*x[i]+b*y[i]"
                    ],
                [
                    lp.ValueArg("a", dtype),
                    lp.GlobalArg("x", dtype, shape="n,"),
                    lp.ValueArg("b", dtype),
                    lp.GlobalArg("y", dtype, shape="n,"),
                    lp.GlobalArg("z", dtype, shape="n,"),
                    lp.ValueArg("n", np.int32, approximately=n),
                    ],
                name="axpy", assumptions="n>=1")

        seq_knl = knl

        def variant_cpu(knl):
            unroll = 16
            block_size = unroll*4096
            knl = lp.split_iname(knl, "i", block_size, outer_tag="g.0", slabs=(0, 1))
            knl = lp.split_iname(knl, "i_inner", unroll, inner_tag="unr")
            return knl

        def variant_gpu(knl):
            unroll = 4
            block_size = 256
            knl = lp.split_iname(knl, "i", unroll*block_size, outer_tag="g.0", slabs=(0, 1))
            knl = lp.split_iname(knl, "i_inner", block_size, outer_tag="unr", inner_tag="l.0")
            return knl

        for variant in [variant_cpu, variant_gpu]:
        #for variant in [ variant_gpu]:
            kernel_gen = lp.generate_loop_schedules(variant(knl))
            kernel_gen = lp.check_kernels(kernel_gen, dict(n=n))

            lp.auto_test_vs_ref(seq_knl, ctx, kernel_gen,
                    op_count=[np.dtype(dtype).itemsize*n*3/1e9],
                    op_label=["GBytes"],
                    parameters={"a": a, "b": b, "n": n}, check_result=check)





def test_transpose(ctx_factory):
    dtype = np.dtype(np.float32)
    ctx = ctx_factory()
    order = "C"

    n = get_suitable_size(ctx)

    knl = lp.make_kernel(ctx.devices[0],
            "{[i,j]: 0<=i,j<%d}" % n,
            [
                "b[i, j] = a[j, i]"
                ],
            [
                lp.GlobalArg("a", dtype, shape=(n, n), order=order),
                lp.GlobalArg("b", dtype, shape=(n, n), order=order),
                ],
            name="transpose")

    seq_knl = knl

    knl = lp.split_iname(knl, "i", 16,
            outer_tag="g.0", inner_tag="l.1")
    knl = lp.split_iname(knl, "j", 16,
            outer_tag="g.1", inner_tag="l.0")
    knl = lp.add_prefetch(knl, 'a', ["i_inner", "j_inner"])

    kernel_gen = lp.generate_loop_schedules(knl)
    kernel_gen = lp.check_kernels(kernel_gen, {})

    lp.auto_test_vs_ref(seq_knl, ctx, kernel_gen,
            op_count=[dtype.itemsize*n**2*2/1e9], op_label=["GByte"],
            parameters={})




def test_plain_matrix_mul(ctx_factory):
    ctx = ctx_factory()
    order = "C"

    n = get_suitable_size(ctx)

    for dtype, check, vec_size in [
            (cl_array.vec.float4, check_float4, 4),
            (np.float32, None, 1),
            ]:
        knl = lp.make_kernel(ctx.devices[0],
                "{[i,j,k]: 0<=i,j,k<%d}" % n,
                [
                    "c[i, j] = sum(k, a[i, k]*b[k, j])"
                    ],
                [
                    lp.GlobalArg("a", dtype, shape=(n, n), order=order),
                    lp.GlobalArg("b", dtype, shape=(n, n), order=order),
                    lp.GlobalArg("c", dtype, shape=(n, n), order=order),
                    ],
                name="matmul")

        ref_knl = knl

        knl = lp.split_iname(knl, "i", 16,
                outer_tag="g.0", inner_tag="l.1")
        knl = lp.split_iname(knl, "j", 16,
                outer_tag="g.1", inner_tag="l.0")
        knl = lp.split_iname(knl, "k", 16)
        knl = lp.add_prefetch(knl, "a", ["k_inner", "i_inner"])
        knl = lp.add_prefetch(knl, "b", ["j_inner", "k_inner", ])

        kernel_gen = lp.generate_loop_schedules(knl)
        kernel_gen = lp.check_kernels(kernel_gen, {})

        lp.auto_test_vs_ref(ref_knl, ctx, kernel_gen,
                op_count=[vec_size*2*n**3/1e9], op_label=["GFlops"],
                parameters={"n": n}, check_result=check)





def test_variable_size_matrix_mul(ctx_factory):
    dtype = np.float32
    ctx = ctx_factory()
    order = "C"

    n = get_suitable_size(ctx)

    knl = lp.make_kernel(ctx.devices[0],
            "[n] -> {[i,j,k]: 0<=i,j,k<n}",
            [
                "c[i, j] = sum(k, a[i, k]*b[k, j]) {id=labl}"
                ],
            [
                lp.GlobalArg("a", dtype, shape="n, n", order=order),
                lp.GlobalArg("b", dtype, shape="n, n", order=order),
                lp.GlobalArg("c", dtype, shape="n, n", order=order),
                lp.ValueArg("n", np.int32, approximately=n),
                ],
            name="matmul", assumptions="n >= 16")

    ref_knl = knl

    knl = lp.split_iname(knl, "i", 16,
            outer_tag="g.0", inner_tag="l.1")
    knl = lp.split_iname(knl, "j", 8,
            outer_tag="g.1", inner_tag="l.0")
    knl = lp.split_iname(knl, "k", 32)

    knl = lp.add_prefetch(knl, "a", ["k_inner", "i_inner"])
    knl = lp.add_prefetch(knl, "b", ["j_inner", "k_inner"])

    kernel_gen = lp.generate_loop_schedules(knl)
    kernel_gen = lp.check_kernels(kernel_gen, dict(n=n))

    lp.auto_test_vs_ref(ref_knl, ctx, kernel_gen,
            op_count=[2*n**3/1e9], op_label=["GFlops"],
            parameters={"n": n})





def test_rank_one(ctx_factory):
    dtype = np.float32
    ctx = ctx_factory()
    order = "F"

    #n = int(get_suitable_size(ctx)**(2.7/2))
    n = 16**3

    knl = lp.make_kernel(ctx.devices[0],
            "[n] -> {[i,j]: 0<=i,j<n}",
            [
                "c[i, j] = a[i]*b[j] {id=mylabel, priority =5}"
                ],
            [
                lp.GlobalArg("a", dtype, shape=("n",), order=order),
                lp.GlobalArg("b", dtype, shape=("n",), order=order),
                lp.GlobalArg("c", dtype, shape=("n, n"), order=order),
                lp.ValueArg("n", np.int32, approximately=n),
                ],
            name="rank_one",
            assumptions="n >= 16")

    def variant_1(knl):
        knl = lp.add_prefetch(knl, "a")
        knl = lp.add_prefetch(knl, "b")
        return knl

    def variant_2(knl):
        knl = lp.split_iname(knl, "i", 16,
                outer_tag="g.0", inner_tag="l.0")
        knl = lp.split_iname(knl, "j", 16,
                outer_tag="g.1", inner_tag="l.1")

        knl = lp.add_prefetch(knl, "a")
        knl = lp.add_prefetch(knl, "b")
        return knl

    def variant_3(knl):
        knl = lp.split_iname(knl, "i", 16,
                outer_tag="g.0", inner_tag="l.0")
        knl = lp.split_iname(knl, "j", 16,
                outer_tag="g.1", inner_tag="l.1")

        knl = lp.add_prefetch(knl, "a", ["i_inner"])
        knl = lp.add_prefetch(knl, "b", ["j_inner"])
        return knl

    def variant_4(knl):
        knl = lp.split_iname(knl, "i", 256,
                outer_tag="g.0", slabs=(0, 1))
        knl = lp.split_iname(knl, "j", 256,
                outer_tag="g.1", slabs=(0, 1))

        knl = lp.add_prefetch(knl, "a", ["i_inner"], default_tag=None)
        knl = lp.add_prefetch(knl, "b", ["j_inner"], default_tag=None)

        knl = lp.split_iname(knl, "i_inner", 16,
                inner_tag="l.0")
        knl = lp.split_iname(knl, "j_inner", 16,
                inner_tag="l.1")

        knl = lp.split_iname(knl, "a_dim_0", 16,
                outer_tag="l.1", inner_tag="l.0")
        knl = lp.split_iname(knl, "b_dim_0", 16,
                outer_tag="l.1", inner_tag="l.0")
        return knl

    seq_knl = knl

    #for variant in [variant_1, variant_2, variant_3, variant_4]:
    for variant in [variant_4]:
        kernel_gen = lp.generate_loop_schedules(variant(knl),
                loop_priority=["i", "j"])
        kernel_gen = lp.check_kernels(kernel_gen, dict(n=n))

        lp.auto_test_vs_ref(seq_knl, ctx, kernel_gen,
                op_count=[np.dtype(dtype).itemsize*n**2/1e9], op_label=["GBytes"],
                parameters={"n": n}, edit_code=True, do_check=False)




def test_troublesome_premagma_fermi_matrix_mul(ctx_factory):
    dtype = np.float32
    ctx = ctx_factory()
    order = "C"

    n = 6*16*2

    knl = lp.make_kernel(ctx.devices[0],
            "{[i,j,k]: 0<=i,j,k<%d}" % n,
            [
                "c[i, j] = sum(k, a[i, k]*b[k, j])"
                ],
            [
                lp.GlobalArg("a", dtype, shape=(n, n), order=order),
                lp.GlobalArg("b", dtype, shape=(n, n), order=order),
                lp.GlobalArg("c", dtype, shape=(n, n), order=order),
                ],
            name="matmul")

    seq_knl = knl

    i_reg = 2
    j_reg = 2
    i_chunks = 16
    j_chunks = 16
    knl = lp.split_iname(knl, "i", i_reg*i_chunks, outer_tag="g.0")
    knl = lp.split_iname(knl, "i_inner", i_reg, outer_tag="l.0", inner_tag="ilp")
    knl = lp.split_iname(knl, "j", j_reg*j_chunks, outer_tag="g.1")
    knl = lp.split_iname(knl, "j_inner", j_reg, outer_tag="l.1", inner_tag="ilp")
    knl = lp.split_iname(knl, "k", 16)
    knl = lp.add_prefetch(knl, 'a', ["k_inner", "i_inner_inner", "i_inner_outer"])

    kernel_gen = lp.generate_loop_schedules(knl)
    kernel_gen = lp.check_kernels(kernel_gen, dict(n=n))

    lp.auto_test_vs_ref(seq_knl, ctx, kernel_gen,
            op_count=[2*n**3/1e9], op_label=["GFlops"],
            parameters={})




def test_intel_matrix_mul(ctx_factory):
    dtype = np.float32
    ctx = ctx_factory()
    order = "C"

    n = 128+32

    knl = lp.make_kernel(ctx.devices[0],
            "{[i,j,k]: 0<=i,j,k<%d}" % n,
            [
                "c[i, j] = sum(k, a[i, k]*b[k, j])"
                ],
            [
                lp.GlobalArg("a", dtype, shape=(n, n), order=order),
                lp.GlobalArg("b", dtype, shape=(n, n), order=order),
                lp.GlobalArg("c", dtype, shape=(n, n), order=order),
                ],
            name="matmul")

    seq_knl = knl

    i_reg = 4
    j_reg = 4
    i_chunks = 16
    j_chunks = 16
    knl = lp.split_iname(knl, "i", i_reg*i_chunks, outer_tag="g.0")
    knl = lp.split_iname(knl, "i_inner", i_reg, outer_tag="l.0", inner_tag="ilp")
    knl = lp.split_iname(knl, "j", j_reg*j_chunks, outer_tag="g.1")
    knl = lp.split_iname(knl, "j_inner", j_reg, outer_tag="l.1", inner_tag="ilp")
    knl = lp.split_iname(knl, "k", 16)
    #knl = lp.split_iname(knl, "k_inner", 8, outer_tag="unr")

    knl = lp.add_prefetch(knl, 'a', ["i_inner_inner", "k_inner", "i_inner_outer"])
    knl = lp.add_prefetch(knl, 'b', ["j_inner_inner", "k_inner", "j_inner_outer"])

    # FIXME: Grouped prefetch
    #knl = lp.add_prefetch(knl, 'a', ["k_inner", ("i_inner_inner", "i_inner_outer")])
    #knl = lp.add_prefetch(knl, 'b', ["k_inner", ("j_inner_inner", "j_inner_outer"),])

    kernel_gen = lp.generate_loop_schedules(knl)
    #hints=["k_outer", "k_inner_outer", "k_inner_inner"]
    kernel_gen = lp.check_kernels(kernel_gen, dict(n=n))

    lp.auto_test_vs_ref(seq_knl, ctx, kernel_gen,
            op_count=[2*n**3/1e9], op_label=["GFlops"],
            parameters={})





def test_magma_fermi_matrix_mul(ctx_factory):
    dtype = np.float32
    ctx = ctx_factory()
    order = "C"

    n = get_suitable_size(ctx)

    knl = lp.make_kernel(ctx.devices[0],
            "{[i,j,k]: 0<=i,j,k<%d}" % n,
            [
                "c[i, j] = sum(k, a[i, k]*b[k, j])"
                ],
            [
                lp.ImageArg("a", dtype, shape=(n, n)),
                lp.ImageArg("b", dtype, shape=(n, n)),
                lp.GlobalArg("c", dtype, shape=(n, n), order=order),
                ],
            name="matmul")

    seq_knl = knl

    i_reg = 4
    j_reg = 4
    i_chunks = 16
    j_chunks = 16


    knl = lp.split_iname(knl, "i", i_reg*i_chunks, outer_tag="g.0")
    knl = lp.split_iname(knl, "i_inner", i_reg, outer_tag="l.0", inner_tag="ilp")
    knl = lp.split_iname(knl, "j", j_reg*j_chunks, outer_tag="g.1")
    knl = lp.split_iname(knl, "j_inner", j_reg, outer_tag="l.1", inner_tag="ilp")
    knl = lp.split_iname(knl, "k", 16)
    knl = lp.split_iname(knl, "k_inner", 8, outer_tag="unr")
    # FIXME
    #knl = lp.add_prefetch(knl, 'a', ["k_inner", "i_inner_inner", "i_inner_outer"])
    #knl = lp.add_prefetch(knl, 'b', ["k_inner", ("j_inner_inner", "j_inner_outer"),])

    kernel_gen = lp.generate_loop_schedules(knl)
    kernel_gen = lp.check_kernels(kernel_gen, dict(n=n))

    lp.auto_test_vs_ref(seq_knl, ctx, kernel_gen,
            op_count=[2*n**3/1e9], op_label=["GFlops"],
            parameters={})





def test_image_matrix_mul(ctx_factory):
    dtype = np.float32
    ctx = ctx_factory()
    order = "C"

    n = get_suitable_size(ctx)

    knl = lp.make_kernel(ctx.devices[0],
            "{[i,j,k]: 0<=i,j,k<%d}" % n,
            [
                "c[i, j] = sum(k, a[i, k]*b[k, j])"
                ],
            [
                lp.ImageArg("a", dtype, shape=(n, n)),
                lp.ImageArg("b", dtype, shape=(n, n)),
                lp.GlobalArg("c", dtype, shape=(n, n), order=order),
                ],
            name="matmul")

    seq_knl = knl

    knl = lp.split_iname(knl, "i", 16, outer_tag="g.0", inner_tag="l.1")
    knl = lp.split_iname(knl, "j", 16, outer_tag="g.1", inner_tag="l.0")
    knl = lp.split_iname(knl, "k", 32)
    # conflict-free
    knl = lp.add_prefetch(knl, 'a', ["i_inner", "k_inner"])
    knl = lp.add_prefetch(knl, 'b', ["j_inner", "k_inner"])

    kernel_gen = lp.generate_loop_schedules(knl)
    kernel_gen = lp.check_kernels(kernel_gen, dict(n=n))

    lp.auto_test_vs_ref(seq_knl, ctx, kernel_gen,
            op_count=[2*n**3/1e9], op_label=["GFlops"],
            parameters={})



def test_image_matrix_mul_ilp(ctx_factory):
    dtype = np.float32
    ctx = ctx_factory()
    order = "C"

    n = get_suitable_size(ctx)

    knl = lp.make_kernel(ctx.devices[0],
            "{[i,j,k]: 0<=i,j,k<%d}" % n,
            [
                "c[i, j] = sum(k, a[i, k]*b[k, j])"
                ],
            [
                lp.ImageArg("a", dtype, shape=(n, n)),
                lp.ImageArg("b", dtype, shape=(n, n)),
                lp.GlobalArg("c", dtype, shape=(n, n), order=order),
                ],
            name="matmul")

    seq_knl = knl

    ilp = 4
    knl = lp.split_iname(knl, "i", 2, outer_tag="g.0", inner_tag="l.1")
    j_inner_split = 4
    knl = lp.split_iname(knl, "j", ilp*j_inner_split, outer_tag="g.1")
    knl = lp.split_iname(knl, "j_inner", j_inner_split, outer_tag="ilp", inner_tag="l.0")
    knl = lp.split_iname(knl, "k", 2)
    # conflict-free?
    knl = lp.add_prefetch(knl, 'a', ["i_inner", "k_inner"])
    knl = lp.add_prefetch(knl, 'b', ["j_inner_outer", "j_inner_inner", "k_inner"])

    kernel_gen = lp.generate_loop_schedules(knl)
    kernel_gen = lp.check_kernels(kernel_gen, dict(n=n))

    lp.auto_test_vs_ref(seq_knl, ctx, kernel_gen,
            op_count=[2*n**3/1e9], op_label=["GFlops"],
            parameters={})



def test_ilp_race_matmul(ctx_factory):
    dtype = np.float32
    ctx = ctx_factory()
    order = "C"

    n = 9

    knl = lp.make_kernel(ctx.devices[0],
            "{[i,j,k]: 0<=i,j,k<%d}" % n,
            [
                "c[i, j] = sum(k, a[i, k]*b[k, j])"
                ],
            [
                lp.ImageArg("a", dtype, shape=(n, n)),
                lp.ImageArg("b", dtype, shape=(n, n)),
                lp.GlobalArg("c", dtype, shape=(n, n), order=order),
                ],
            name="matmul")

    knl = lp.split_iname(knl, "j", 2, outer_tag="ilp", inner_tag="l.0")
    knl = lp.split_iname(knl, "k", 2)
    knl = lp.add_prefetch(knl, 'b', ["k_inner"])

    from loopy.check import WriteRaceConditionError
    import pytest
    with pytest.raises(WriteRaceConditionError):
        list(lp.generate_loop_schedules(knl))





def test_fancy_matrix_mul(ctx_factory):
    dtype = np.float32
    ctx = ctx_factory()

    order = "C"

    n = get_suitable_size(ctx)

    knl = lp.make_kernel(ctx.devices[0],
            "[n] -> {[i,j,k]: 0<=i,j,k<n }",
            [
                "c[i, j] = sum(k, a[i, k]*b[k, j])"
                ],
            [
                lp.GlobalArg("a", dtype, shape="(n, n)", order=order),
                lp.GlobalArg("b", dtype, shape="(n, n)", order=order),
                lp.GlobalArg("c", dtype, shape="(n, n)", order=order),
                lp.ValueArg("n", np.int32, approximately=1000),
                ], name="fancy_matmul", assumptions="n>=1")

    seq_knl = knl

    knl = lp.split_iname(knl, "i", 16, outer_tag="g.0", inner_tag="l.1")
    knl = lp.split_iname(knl, "j", 16, outer_tag="g.1", inner_tag="l.0")
    knl = lp.split_iname(knl, "k", 16, slabs=(0,1))
    knl = lp.add_prefetch(knl, 'a', ["i_inner", "k_inner"])
    knl = lp.add_prefetch(knl, 'b', ["k_inner", "j_inner"])

    kernel_gen = lp.generate_loop_schedules(knl)
    kernel_gen = lp.check_kernels(kernel_gen, dict(n=n))

    lp.auto_test_vs_ref(seq_knl, ctx, kernel_gen,
            op_count=[2*n**3/1e9], op_label=["GFlops"],
            parameters=dict(n=n))





def test_small_batched_matvec(ctx_factory):
    dtype = np.float32
    ctx = ctx_factory()

    order = "C"

    K = 9997
    Np = 36

    knl = lp.make_kernel(ctx.devices[0],
            "[K] -> {[i,j,k]: 0<=k<K and 0<= i,j < %d}" % Np,
            [
                "result[k, i] = sum(j, d[i, j]*f[k, j])"
                ],
            [
                lp.GlobalArg("d", dtype, shape=(Np, Np), order=order),
                lp.GlobalArg("f", dtype, shape=("K", Np), order=order),
                lp.GlobalArg("result", dtype, shape=("K", Np), order=order),
                lp.ValueArg("K", np.int32, approximately=1000),
                ], name="batched_matvec", assumptions="K>=1")

    seq_knl = knl

    align_bytes = 64
    knl = lp.add_prefetch(knl, 'd[:,:]')
    pad_mult = lp.find_padding_multiple(knl, "f", 0, align_bytes)
    knl = lp.split_arg_axis(knl, ("f", 0), pad_mult)
    knl = lp.add_padding(knl, "f", 0, align_bytes)

    kernel_gen = lp.generate_loop_schedules(knl)
    kernel_gen = lp.check_kernels(kernel_gen, dict(K=K))

    lp.auto_test_vs_ref(seq_knl, ctx, kernel_gen,
            op_count=[K*2*Np**2/1e9], op_label=["GFlops"],
            parameters=dict(K=K))




if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        exec(sys.argv[1])
    else:
        from py.test.cmdline import main
        main([__file__])
