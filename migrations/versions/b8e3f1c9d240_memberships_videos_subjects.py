"""memberships, owner videos, and product subjects

Revision ID: b8e3f1c9d240
Revises: a7d2e9c14b83
Create Date: 2026-07-15 13:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b8e3f1c9d240'
down_revision = 'a7d2e9c14b83'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('membership', sa.String(length=20),
                                      nullable=False, server_default='none'))
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(sa.Column('subject', sa.String(length=60), nullable=True))

    op.create_table(
        'videos',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=160), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('filename', sa.String(length=255), nullable=True),
        sa.Column('mime', sa.String(length=120), nullable=False),
        sa.Column('size', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('data', sa.LargeBinary(), nullable=False),
        sa.Column('thumb_data', sa.LargeBinary(), nullable=True),
        sa.Column('thumb_mime', sa.String(length=40), nullable=True),
        sa.Column('published', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('videos')
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_column('subject')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('membership')
