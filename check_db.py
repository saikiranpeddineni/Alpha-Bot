with open('backend/app/core/engine.py') as f:
    lines = f.readlines()
for i, line in enumerate(lines):
    if '.EXIT' in line:
        print(f'Line {i+1}: {line.strip()}')
