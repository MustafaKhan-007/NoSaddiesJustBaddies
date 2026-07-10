"""profile links (Instagram, own courses, etc.)

Revision ID: d1a4f6b82c30
Revises: c9e5a7b30f18
Create Date: 2026-07-10 19:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd1a4f6b82c30'
down_revision = 'c9e5a7b30f18'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('links_json', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('links_json')
