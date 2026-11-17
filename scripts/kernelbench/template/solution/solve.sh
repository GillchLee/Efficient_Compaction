#!/bin/bash
# Oracle: the PyTorch reference itself as ModelNew (correct, speedup ~1.0).
cp /solution/reference.py /workspace/solution.py
printf "\nclass ModelNew(Model):\n    pass\n" >> /workspace/solution.py
