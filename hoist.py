import re

with open("backend/app/backtest/engine.py", "r") as f:
    lines = f.readlines()

imports = set()
new_lines = []

for line in lines:
    m = re.match(r"^(\s+)(import\s+.*|from\s+.*import\s+.*)$", line)
    if m:
        indent = m.group(1)
        imp = m.group(2)
        
        # Don't hoist standard imports if we don't want to, but it's safe to hoist all of them
        # Except if they are in try/except!
        # Let's just hoist everything except those in try/except blocks if possible.
        # Wait, the try/except block for NewsClient is specific.
        if "alpaca.data.historical" in imp or "alpaca.data.requests" in imp:
            new_lines.append(line)
            continue
            
        imports.add(imp)
        # We don't append it to new_lines, effectively removing it
    else:
        new_lines.append(line)

top_imports = list(imports)
top_imports.sort()

# Find the last top-level import in engine.py to insert our hoisted imports
insert_idx = 0
for i, line in enumerate(new_lines):
    if line.startswith("from app.ai.gate_report import build_gate_value_report"):
        insert_idx = i + 1
        break

new_lines.insert(insert_idx, "\n# Hoisted from local scopes by Antigravity\n" + "\n".join(top_imports) + "\n")

with open("backend/app/backtest/engine.py", "w") as f:
    f.writelines(new_lines)

print("Hoisted", len(top_imports), "imports.")
