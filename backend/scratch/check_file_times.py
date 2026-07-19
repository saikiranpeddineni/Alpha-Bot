import os
from datetime import datetime

meta_dir = 'data/models/meta'
try:
    files = os.listdir(meta_dir)
    print("==================================================")
    print(" META-MODEL FILE MODIFICATION TIMESTAMPS")
    print("==================================================")
    for f in sorted(files):
        path = os.path.join(meta_dir, f)
        mtime = os.path.getmtime(path)
        dt = datetime.fromtimestamp(mtime)
        print(f" - {f:<30} | Modified: {dt.isoformat()}")
    print("==================================================")
except Exception as e:
    print(f"Error: {e}")
