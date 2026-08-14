import re
import sys

def hoist_file(filepath):
    with open(filepath, "r") as f:
        lines = f.readlines()

    imports = set()
    new_lines = []

    for line in lines:
        m = re.match(r"^(\s+)(import\s+.*|from\s+.*import\s+.*)$", line)
        if m:
            indent = m.group(1)
            imp = m.group(2)
            imports.add(imp)
        else:
            new_lines.append(line)

    if not imports:
        print(f"No imports hoisted in {filepath}")
        return

    top_imports = list(imports)
    top_imports.sort()

    # Insert after the last top-level import
    insert_idx = 0
    for i, line in enumerate(new_lines):
        if line.startswith("import ") or line.startswith("from "):
            insert_idx = i + 1

    if insert_idx == 0:
        insert_idx = 1 # after __future__ or something

    new_lines.insert(insert_idx, "\n# Hoisted from local scopes by Antigravity\n" + "\n".join(top_imports) + "\n")

    with open(filepath, "w") as f:
        f.writelines(new_lines)
        
    print(f"Hoisted {len(top_imports)} imports in {filepath}")

for path in sys.argv[1:]:
    hoist_file(path)
