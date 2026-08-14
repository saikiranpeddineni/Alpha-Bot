import sys
sys.path.insert(0, "backend")
from app.config import DATA_DIR
from app.replay.session import ReplaySessionManager

print("DATA_DIR:", DATA_DIR)
sm = ReplaySessionManager()
sess = sm.get_session("rpl_7ce7723410e8")
print("Session:", sess)
