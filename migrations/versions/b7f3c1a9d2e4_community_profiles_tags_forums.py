"""community forums, member profiles, course tags; drop check-ins

Revision ID: b7f3c1a9d2e4
Revises: 4d7b2c5a0e9c
Create Date: 2026-07-07 18:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7f3c1a9d2e4'
down_revision = '4d7b2c5a0e9c'
branch_labels = None
depends_on = None


def upgrade():
    # --- member profile + moderation fields ---------------------------------
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('avatar_url', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('bio', sa.String(length=400), nullable=True))
        batch_op.add_column(sa.Column('goals_json', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('default_anonymous', sa.Boolean(),
                                      nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('forum_warnings', sa.Integer(),
                                      nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('forum_banned', sa.Boolean(),
                                      nullable=False, server_default=sa.false()))

    # --- hidden recommendation tags on products -----------------------------
    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tags_json', sa.Text(), nullable=True))

    # --- forums --------------------------------------------------------------
    op.create_table(
        'forum_categories',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('slug', sa.String(length=60), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('description', sa.String(length=240), nullable=False),
        sa.Column('accent', sa.String(length=7), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('slug'),
    )
    op.create_table(
        'forum_posts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('category_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=160), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('anonymous', sa.Boolean(), nullable=False),
        sa.Column('hidden', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['category_id'], ['forum_categories.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('forum_posts', schema=None) as batch_op:
        batch_op.create_index('ix_forum_posts_category_id', ['category_id'], unique=False)

    op.create_table(
        'forum_comments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('post_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('anonymous', sa.Boolean(), nullable=False),
        sa.Column('hidden', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['post_id'], ['forum_posts.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('forum_comments', schema=None) as batch_op:
        batch_op.create_index('ix_forum_comments_post_id', ['post_id'], unique=False)

    op.create_table(
        'forum_post_likes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('post_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['post_id'], ['forum_posts.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'post_id', name='uq_postlike_user_post'),
    )
    op.create_table(
        'forum_comment_likes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('comment_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['comment_id'], ['forum_comments.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'comment_id', name='uq_commentlike_user_comment'),
    )

    # --- retire the check-in / streak system --------------------------------
    op.drop_table('check_ins')


def downgrade():
    op.create_table(
        'check_ins',
        sa.Column('id', sa.INTEGER(), nullable=False),
        sa.Column('user_id', sa.INTEGER(), nullable=False),
        sa.Column('date', sa.DATE(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'date', name='uq_checkin_user_date'),
    )

    op.drop_table('forum_comment_likes')
    op.drop_table('forum_post_likes')
    with op.batch_alter_table('forum_comments', schema=None) as batch_op:
        batch_op.drop_index('ix_forum_comments_post_id')
    op.drop_table('forum_comments')
    with op.batch_alter_table('forum_posts', schema=None) as batch_op:
        batch_op.drop_index('ix_forum_posts_category_id')
    op.drop_table('forum_posts')
    op.drop_table('forum_categories')

    with op.batch_alter_table('products', schema=None) as batch_op:
        batch_op.drop_column('tags_json')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('forum_banned')
        batch_op.drop_column('forum_warnings')
        batch_op.drop_column('default_anonymous')
        batch_op.drop_column('goals_json')
        batch_op.drop_column('bio')
        batch_op.drop_column('avatar_url')
