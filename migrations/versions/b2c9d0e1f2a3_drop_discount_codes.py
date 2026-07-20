"""drop discount_codes (handled in Lemon Squeezy only)

Revision ID: b2c9d0e1f2a3
Revises: a1b8c4d5e6f7
Create Date: 2026-07-20 07:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'b2c9d0e1f2a3'
down_revision = 'a1b8c4d5e6f7'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table('discount_codes')


def downgrade():
    op.create_table(
        'discount_codes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=60), nullable=False),
        sa.Column('label', sa.String(length=120), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code'),
    )
