from pathlib import Path
import sys
from core.scanner import scan
from generator.compiler import compile_lvgl, write_aibuilder_custom

source = Path(sys.argv[1]); destination = Path(sys.argv[2])
model = scan(source)
bundle = compile_lvgl(model)
write_aibuilder_custom(bundle, destination, model=model, include_runtime=False)
print(f"components={len(model.components)} units={len(bundle.units)}", flush=True)
