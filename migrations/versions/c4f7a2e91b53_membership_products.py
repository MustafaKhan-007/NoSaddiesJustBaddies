"""products can grant a membership tier when purchased

Revision ID: c4f7a2e91b53
Revises: b8e3f1c9d240
Create Date: 2026-07-15 14:05:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4f7a2e91b53'
down_revision = 'b8e3f1c9d240'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(sa.Column('grants_membership', sa.String(length=20), nullable=True))


def downgrade():
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_column('grants_membership')
