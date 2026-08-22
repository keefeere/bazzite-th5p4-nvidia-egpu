#!/usr/bin/env python3
"""Measure pinned host<->RTX bandwidth through the CUDA driver API."""

from __future__ import annotations

import ctypes as C
import pathlib
import time


CUDA = C.CDLL("libcuda.so.1")
CUdevice = C.c_int
CUcontext = C.c_void_p
CUdeviceptr = C.c_uint64


def bind(name: str, restype: object, *argtypes: object):
    fn = getattr(CUDA, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


cuInit = bind("cuInit", C.c_int, C.c_uint)
cuDeviceGetCount = bind("cuDeviceGetCount", C.c_int, C.POINTER(C.c_int))
cuDeviceGet = bind("cuDeviceGet", C.c_int, C.POINTER(CUdevice), C.c_int)
cuDeviceGetName = bind("cuDeviceGetName", C.c_int, C.c_char_p, C.c_int, CUdevice)
cuCtxCreate = bind("cuCtxCreate_v2", C.c_int, C.POINTER(CUcontext), C.c_uint, CUdevice)
cuCtxDestroy = bind("cuCtxDestroy_v2", C.c_int, CUcontext)
cuMemHostAlloc = bind("cuMemHostAlloc", C.c_int, C.POINTER(C.c_void_p), C.c_size_t, C.c_uint)
cuMemFreeHost = bind("cuMemFreeHost", C.c_int, C.c_void_p)
cuMemAlloc = bind("cuMemAlloc_v2", C.c_int, C.POINTER(CUdeviceptr), C.c_size_t)
cuMemFree = bind("cuMemFree_v2", C.c_int, CUdeviceptr)
cuMemcpyHtoD = bind("cuMemcpyHtoD_v2", C.c_int, CUdeviceptr, C.c_void_p, C.c_size_t)
cuMemcpyDtoH = bind("cuMemcpyDtoH_v2", C.c_int, C.c_void_p, CUdeviceptr, C.c_size_t)
cuGetErrorName = bind("cuGetErrorName", C.c_int, C.c_int, C.POINTER(C.c_char_p))
cuGetErrorString = bind("cuGetErrorString", C.c_int, C.c_int, C.POINTER(C.c_char_p))


def check(rc: int, what: str) -> None:
    if rc == 0:
        return
    name = C.c_char_p()
    message = C.c_char_p()
    cuGetErrorName(rc, C.byref(name))
    cuGetErrorString(rc, C.byref(message))
    raise RuntimeError(
        f"{what}: {name.value.decode() if name.value else rc}: "
        f"{message.value.decode() if message.value else 'unknown CUDA error'}"
    )


def sysfs_text(name: str) -> str:
    path = pathlib.Path("/sys/bus/pci/devices/0000:03:00.0") / name
    try:
        return path.read_text().strip()
    except OSError:
        return "unknown"


def timed_copy(fn, *args: object, iterations: int, size: int) -> float:
    start = time.perf_counter()
    for _ in range(iterations):
        check(fn(*args, size), fn.__name__)
    elapsed = time.perf_counter() - start
    return size * iterations / elapsed / 1_000_000_000


def main() -> None:
    size = 256 * 1024 * 1024
    iterations = 8
    count = C.c_int()
    device = CUdevice()
    context = CUcontext()
    host = C.c_void_p()
    gpu = CUdeviceptr()

    check(cuInit(0), "cuInit")
    check(cuDeviceGetCount(C.byref(count)), "cuDeviceGetCount")
    if count.value < 1:
        raise RuntimeError("CUDA reports no GPU")
    check(cuDeviceGet(C.byref(device), 0), "cuDeviceGet")
    name = C.create_string_buffer(256)
    check(cuDeviceGetName(name, len(name), device), "cuDeviceGetName")
    check(cuCtxCreate(C.byref(context), 0, device), "cuCtxCreate")

    try:
        check(cuMemHostAlloc(C.byref(host), size, 0), "cuMemHostAlloc")
        try:
            check(cuMemAlloc(C.byref(gpu), size), "cuMemAlloc")
            try:
                C.memset(host, 0xA5, size)
                check(cuMemcpyHtoD(gpu, host, size), "warm-up HtoD")
                check(cuMemcpyDtoH(host, gpu, size), "warm-up DtoH")

                h2d = timed_copy(
                    cuMemcpyHtoD, gpu, host, iterations=iterations, size=size
                )
                d2h = timed_copy(
                    cuMemcpyDtoH, host, gpu, iterations=iterations, size=size
                )

                print(f"GPU: {name.value.decode()}")
                print(
                    "PCIe link: "
                    f"{sysfs_text('current_link_speed')} x{sysfs_text('current_link_width')}"
                )
                print(f"Transfer size: {size // (1024 * 1024)} MiB x {iterations}")
                print(f"Pinned host -> device: {h2d:.3f} GB/s")
                print(f"Device -> pinned host: {d2h:.3f} GB/s")
            finally:
                check(cuMemFree(gpu), "cuMemFree")
        finally:
            check(cuMemFreeHost(host), "cuMemFreeHost")
    finally:
        check(cuCtxDestroy(context), "cuCtxDestroy")


if __name__ == "__main__":
    main()
