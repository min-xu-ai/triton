"""
Fused Softmax
=================
In this tutorial, you will write a fused softmax operation that is significantly faster
than PyTorch's native op for a particular class of matrices: those whose rows can fit in
the GPU's SRAM.
You will learn about:

- The benefits of kernel fusion for bandwidth-bound operations.
- Reduction operators in Triton.
"""

# %%
# Motivations
# ------------
# Custom GPU kernels for elementwise additions are educationally valuable but won't get you very far in practice.
# Let us consider instead the case of a simple (numerically stabilized) softmax operation:

import torch


@torch.jit.script
def naive_softmax(x):
    """Compute row-wise softmax of X using native pytorch

    We subtract the maximum element in order to avoid overflows. Softmax is invariant to
    this shift.
    """
    # read  MN elements ; write M  elements
    x_max = x.max(dim=1)[0]
    # read MN + M elements ; write MN elements
    z = x - x_max[:, None]
    # read  MN elements ; write MN elements
    numerator = torch.exp(z)
    # read  MN elements ; write M  elements
    denominator = numerator.sum(dim=1)
    # read MN + M elements ; write MN elements
    ret = numerator / denominator[:, None]
    # in total: read 5MN + 2M elements ; wrote 3MN + 2M elements
    return ret


# %%
# When implemented naively in PyTorch, computing :code:`y = naive_softmax(x)` for :math:`x \in R^{M \times N}`
# requires reading :math:`5MN + 2M` elements from DRAM and writing back :math:`3MN + 2M` elements.
# This is obviously wasteful; we'd prefer to have a custom "fused" kernel that only reads
# X once and does all the necessary computations on-chip.
# Doing so would require reading and writing back only :math:`MN` bytes, so we could
# expect a theoretical speed-up of ~4x (i.e., :math:`(8MN + 4M) / 2MN`).
# The `torch.jit.script` flags aims to perform this kind of "kernel fusion" automatically
# but, as we will see later, it is still far from ideal.

# %%
# Compute Kernel
# ----------------
# Our softmax kernel works as follows: each program loads a row of the input matrix X,
# normalizes it and writes back the result to the output Y.
# Note that one important limitation of Triton is that each block must have a
# power-of-two number of elements, so we need to internally "pad" each row and guard the
# memory operations properly if we want to handle any possible input shapes:

import triton
import triton.language as tl


@triton.jit
def softmax_kernel(
    output_ptr, input_ptr, input_row_stride, output_row_stride, n_cols, **meta
):
    # The rows of the softmax are independent, so we parallelize across those
    row_idx = tl.program_id(0)
    BLOCK_SIZE = meta['BLOCK_SIZE']
    # The stride represents how much we need to increase the pointer to advance 1 row
    row_start_ptr = input_ptr + row_idx * input_row_stride
    # The block size is the next power of two greater than n_cols, so we can fit each
    # row in a single block
    col_offsets = tl.arange(0, BLOCK_SIZE)
    input_ptrs = row_start_ptr + col_offsets
    # Load the row into SRAM, using a mask since BLOCK_SIZE may be > than n_cols
    row = tl.load(input_ptrs, mask=col_offsets < n_cols, other=-float('inf'))
    # Substract maximum for numerical stability
    row_minus_max = row - tl.max(row, axis=0)
    # Note that exponentials in Triton are fast but approximate (i.e., think __expf in CUDA)
    numerator = tl.exp(row_minus_max)
    denominator = tl.sum(numerator, axis=0)
    softmax_output = numerator / denominator
    # Write back output to DRAM
    output_row_start_ptr = output_ptr + row_idx * output_row_stride
    output_ptrs = output_row_start_ptr + col_offsets
    tl.store(output_ptrs, softmax_output, mask=col_offsets < n_cols)

@triton.jit
def softmax_kernel_big_row(
    output_ptr, input_ptr, input_row_stride, output_row_stride, n_cols, **meta
):
    """ Specialized kernel for big row size inputs.

        issues & todos:
          - can't use loop
          - can't use if conditions
          - could potentially speed up more if we can do exp_sum() using float8
    """
    row_idx = tl.program_id(0)

    # The stride represents how much we need to increase the pointer to advance 1 row
    row_start_ptr = input_ptr + row_idx * input_row_stride

    N_BLOCKS = 2
    BLOCK_SIZE = meta['BLOCK_SIZE'] // 2

    # Having this computed inside loop doesn't work. Error out on "BLOCK_SIZE" is phi
    # node, not an int.
    col_offsets_base = tl.arange(0, BLOCK_SIZE)

    # Make row a global variable that's outside of the loops
    row = tl.zeros((BLOCK_SIZE,), dtype=tl.float32)

    # all blocks.
    tl_max = float('-inf')
    for col_start in range(0, N_BLOCKS*BLOCK_SIZE, BLOCK_SIZE):
        col_offsets = col_offsets_base + col_start
        input_ptrs = row_start_ptr + col_offsets
        row = tl.load(input_ptrs, mask=col_offsets < n_cols, other=-float('inf'))
        tl_max = tl.maximum(tl_max, tl.max(row, axis=0))

    # last block
    tl_sum = tl.sum(tl.exp(row - tl_max), axis=0)
    # rest
    for col_start in range((N_BLOCKS-2)*BLOCK_SIZE, -1, -BLOCK_SIZE):
        col_offsets = col_offsets_base + col_start
        input_ptrs = row_start_ptr + col_offsets
        row = tl.load(input_ptrs, mask=col_offsets < n_cols, other=-float('inf'))
        tl_sum += tl.sum(tl.exp(row - tl_max), axis=0)

    output_row_start_ptr = output_ptr + row_idx * output_row_stride

    # col_start == 0
    col_offsets = col_offsets_base
    softmax_output = tl.exp(row - tl_max) / tl_sum
    output_ptrs = output_row_start_ptr + col_offsets
    tl.store(output_ptrs, softmax_output, mask=col_offsets < n_cols)

    # rest
    for col_start in range(BLOCK_SIZE, N_BLOCKS*BLOCK_SIZE, BLOCK_SIZE):
        col_offsets = col_offsets_base + col_start
        input_ptrs = row_start_ptr + col_offsets
        row = tl.load(input_ptrs, mask=col_offsets < n_cols, other=-float('inf'))
        softmax_output = tl.exp(row - tl_max) / tl_sum
        output_ptrs = output_row_start_ptr + col_offsets
        tl.store(output_ptrs, softmax_output, mask=col_offsets < n_cols)


# %%
# We can create a helper function that enqueues the kernel and its (meta-)arguments for any given input tensor.

def softmax(x):
    n_rows, n_cols = x.shape
    # The block size is the smallest power of two greater than the number of columns in `x`
    BLOCK_SIZE = triton.next_power_of_2(n_cols)
    # Another trick we can use is to ask the compiler to use more threads per row by
    # increasing the number of warps (`num_warps`) over which each row is distributed.
    # You will see in the next tutorial how to auto-tune this value in a more natural
    # way so you don't have to come up with manual heuristics yourself.
    num_warps = 4
    if BLOCK_SIZE >= 2048:
        num_warps = 8
    if BLOCK_SIZE >= 4096:
        num_warps = 16
    if BLOCK_SIZE >= 8096:
        num_warps = 32
    k = softmax_kernel
    if True and BLOCK_SIZE >= 64*1024:
        k = softmax_kernel_big_row
    # Allocate output
    y = torch.empty_like(x)
    # Enqueue kernel. The 1D launch grid is simple: we have one kernel instance per row o
    # f the input matrix
    k[(n_rows,)](
        y,
        x,
        x.stride(0),
        y.stride(0),
        n_cols,
        num_warps=num_warps,
        BLOCK_SIZE=BLOCK_SIZE,
    )
    return y


# %%
# Unit Test
# ----------

# %%
# We make sure that we test our kernel on a matrix with an irregular number of rows and columns.
# This will allow us to verify that our padding mechanism works.

torch.manual_seed(0)
x = torch.randn(1823, 781, device='cuda')
y_triton = softmax(x)
y_torch = torch.softmax(x, axis=1)
print("small row result: ", torch.allclose(y_triton, y_torch))
x = torch.randn(1823, 64*1024+11, device='cuda')
y_triton = softmax(x)
y_torch = torch.softmax(x, axis=1)
print("big row result: ", torch.allclose(y_triton, y_torch))

#%%
# As expected, the results are identical.

# %%
# Benchmark
# -------------
# Here we will benchmark our operation as a function of the number of columns in the input matrix -- assuming 4096 rows.
# We will then compare its performance against (1) :code:`torch.softmax` and (2) the :code:`naive_softmax` defined above.


@triton.testing.perf_report(
    triton.testing.Benchmark(
        x_names=['N'],  # argument names to use as an x-axis for the plot
        x_vals=[
            128 * i for i in (list(range(99, 100)) + [512])
        ],  # different possible values for `x_name`
        line_arg='provider',  # argument name whose value corresponds to a different line in the plot
        line_vals=[
            'triton',
            'torch-native',
            'torch-jit',
        ],  # possible values for `line_arg``
        line_names=[
            "Triton",
            "Torch (native)",
            "Torch (jit)",
        ],  # label name for the lines
        styles=[('blue', '-'), ('green', '-'), ('green', '--')],  # line styles
        ylabel="GB/s",  # label name for the y-axis
        plot_name="softmax-performance",  # name for the plot. Used also as a file name for saving the plot.
        args={'M': 4096},  # values for function arguments not in `x_names` and `y_name`
    )
)
def benchmark(M, N, provider):
    x = torch.randn(M, N, device='cuda', dtype=torch.float32)
    if provider == 'torch-native':
        ms, min_ms, max_ms = triton.testing.do_bench(lambda: torch.softmax(x, axis=-1), rep=100, warmup=25)
    if provider == 'triton':
        ms, min_ms, max_ms = triton.testing.do_bench(lambda: softmax(x), rep=100, warmup=25)
    if provider == 'torch-jit':
        ms, min_ms, max_ms = triton.testing.do_bench(lambda: naive_softmax(x), rep=1, warmup=1)
    gbps = lambda ms: 2 * x.nelement() * x.element_size() * 1e-9 / (ms * 1e-3)
    return gbps(ms), gbps(max_ms), gbps(min_ms)


benchmark.run(show_plots=True, print_data=True)

# %%
# In the above plot, we can see that:
#
#  - Triton is 4x faster than the Torch JIT. This confirms our suspicions that the Torch JIT does not do any fusion here.
#  - Triton is noticeably faster than :code:`torch.softmax` -- in addition to being **easier to read, understand and maintain**. 
#    Note however that the PyTorch `softmax` operation is more general and will works on tensors of any shape.
