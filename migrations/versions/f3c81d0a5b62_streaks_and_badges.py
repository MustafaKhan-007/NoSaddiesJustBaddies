"""streaks (check-ins) + displayed badges

Revision ID: f3c81d0a5b62
Revises: e2b6c9147a55
Create Date: 2026-07-11 04:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f3c81d0a5b62'
down_revision = 'e2b6c9147a55'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('last_checkin_date', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('current_streak', sa.Integer(), nullable=False,
                                      server_default='0'))
        batch_op.add_column(sa.Column('longest_streak', sa.Integer(), nullable=False,
                                      server_default='0'))
        batch_op.add_column(sa.Column('total_checkins', sa.Integer(), nullable=False,
                                      server_default='0'))
        batch_op.add_column(sa.Column('displayed_badges_json', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('displayed_badges_json')
        batch_op.drop_column('total_checkins')
        batch_op.drop_column('longest_streak')
        batch_op.drop_column('current_streak')
        batch_op.drop_column('last_checkin_date')
