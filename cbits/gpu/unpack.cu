#include "unpack.hpp"
#include <ATen/cuda/CUDAContext.h>
#include <ATen/cuda/detail/KernelUtils.h>

TCM_NAMESPACE_BEGIN

namespace detail {

struct SpinsInfo {
    uint64_t const* const data;
    int32_t const         stride;
};

struct OutInfo {
    float* const  data;
    int32_t const shape[2];
    int32_t const stride[2];
};

__device__ inline auto unpack_one(uint64_t bits, int32_t const count, float* const out,
                                  int32_t const stride) noexcept -> void
{
    for (auto i = 0; i < count; ++i, bits >>= 1) {
        out[i * stride] = 2.0f * static_cast<float>(bits & 0x01) - 1.0f;
    }
}

__device__ inline auto unpack_word(uint64_t bits, float* const out, int32_t const stride) noexcept
    -> void
{
    for (auto i = 0; i < 64; ++i, bits >>= 1) {
        out[i * stride] = 2.0f * static_cast<float>(bits & 0x01) - 1.0f;
    }
}

__device__ inline auto unpack_one(uint64_t const bits[], int32_t const count, float* out,
                                  int32_t const stride) noexcept -> void
{
    constexpr auto block = 64;

    auto i = 0;
    for (; i < count / block; ++i, out += block * stride) {
        unpack_word(bits[i], out, stride);
    }
    {
        auto const rest = count % block;
        if (rest != 0) {
            unpack_one(bits[i], rest, out, stride);
            // out += rest * stride;
        }
    }
}

__global__ auto unpack_kernel_cuda(TensorInfo<uint64_t const, 2> const spins,
                                   TensorInfo<float, 2> const          out) -> void
{
    auto const idx    = blockIdx.x * blockDim.x + threadIdx.x;
    auto const stride = blockDim.x * gridDim.x;
    for (auto i = idx; i < out.sizes[0]; i += stride) {
        unpack_one(spins.data[i * spins.strides[0]], out.sizes[1], out.data + i * out.strides[0],
                   out.strides[1]);
    }
}
} // namespace detail

auto unpack_cuda(TensorInfo<uint64_t const, 2> const& spins, TensorInfo<float, 2> const& out,
                 c10::Device const device) -> void
{
    // clang-format off
    cudaSetDevice(device.index());
    auto stream = at::cuda::getCurrentCUDAStream();
    detail::unpack_kernel_cuda<<<at::cuda::detail::GET_BLOCKS(out.sizes[0]),
        at::cuda::detail::CUDA_NUM_THREADS, 0, stream>>>(spins, out);
    // clang-format on
}

TCM_NAMESPACE_END
