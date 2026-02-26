docker run --gpus all -it --rm --shm-size=8g \
  --entrypoint /bin/bash \
  -e TORCH_CUDA_ARCH_LIST="9.0" \
  -v "$(pwd)":/workspace \
  -v "$HOME/.nv:/root/.nv" \
  -v "$HOME/.cache:/root/.cache" \
  -w /workspace \
  nvcr.io/nvidia/pytorch:24.12-py3
