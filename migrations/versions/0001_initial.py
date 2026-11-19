from pathlib import Path
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    sql = Path(__file__).with_name("0001.sql").read_text(encoding="utf-8")
    op.get_bind().exec_driver_sql(sql)

def downgrade():
    raise RuntimeError("首版结构请从已核对的备份恢复。")
