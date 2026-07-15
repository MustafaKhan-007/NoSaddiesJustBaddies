"""store video files on disk (add disk_name, make data nullable)

Revision ID: e7f1a3d9c264
Revises: d5a9c3f27e81
Create Date: 2026-07-16 01:15:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e7f1a3d9c264'
down_revision = 'd5a9c3f27e81'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('videos', schema=None) as batch_op:
        batch_op.add_column(sa.Column('disk_name', sa.String(length=64), nullable=True))
        batch_op.alter_column('data', existing_type=sa.LargeBinary(), nullable=True)


def downgrade():
    with op.batch_alter_table('videos', schema=None) as batch_op:
        batch_op.alter_column('data', existing_type=sa.LargeBinary(), nullable=False)
        batch_op.drop_column('disk_name')
