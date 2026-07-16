"""marketplace, multiple announcements, gift orders

Revision ID: f9c2b7e410a8
Revises: e7f1a3d9c264
Create Date: 2026-07-16 02:20:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f9c2b7e410a8'
down_revision = 'e7f1a3d9c264'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('gift_to_email', sa.String(length=255), nullable=True))
        batch_op.create_index('ix_orders_gift_to_email', ['gift_to_email'], unique=False)

    op.create_table(
        'announcements',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('body', sa.String(length=300), nullable=False),
        sa.Column('expires', sa.Date(), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'marketplace_listings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False, server_default='product'),
        sa.Column('title', sa.String(length=140), nullable=False),
        sa.Column('description', sa.Text(), nullable=False, server_default=''),
        sa.Column('location', sa.String(length=120), nullable=True),
        sa.Column('price', sa.String(length=80), nullable=True),
        sa.Column('website_url', sa.String(length=500), nullable=False),
        sa.Column('tags_json', sa.Text(), nullable=True),
        sa.Column('clicks', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_marketplace_listings_user_id', 'marketplace_listings',
                    ['user_id'], unique=False)

    op.create_table(
        'listing_images',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('listing_id', sa.Integer(), nullable=False),
        sa.Column('data', sa.LargeBinary(), nullable=False),
        sa.Column('mime', sa.String(length=40), nullable=False, server_default='image/jpeg'),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.ForeignKeyConstraint(['listing_id'], ['marketplace_listings.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_listing_images_listing_id', 'listing_images',
                    ['listing_id'], unique=False)


def downgrade():
    op.drop_index('ix_listing_images_listing_id', table_name='listing_images')
    op.drop_table('listing_images')
    op.drop_index('ix_marketplace_listings_user_id', table_name='marketplace_listings')
    op.drop_table('marketplace_listings')
    op.drop_table('announcements')
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.drop_index('ix_orders_gift_to_email')
        batch_op.drop_column('gift_to_email')
