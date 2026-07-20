"""store reel-review raw videos in the database

Revision ID: c3d0e1f2a4b5
Revises: b2c9d0e1f2a3
Create Date: 2026-07-21 00:12:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'c3d0e1f2a4b5'
down_revision = 'b2c9d0e1f2a3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('reel_review_applications', schema=None) as batch_op:
        batch_op.add_column(sa.Column('data', sa.LargeBinary(), nullable=True))


def downgrade():
    with op.batch_alter_table('reel_review_applications', schema=None) as batch_op:
        batch_op.drop_column('data')
