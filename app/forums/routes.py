"""Community forums: Reddit-style categories, posts, comments and likes.

- Members can post and comment (optionally anonymously).
- A profanity guard blocks unkind language, warns, then removes posting access.
- Likes are one-per-member toggles, mirroring the quote "heart" pattern.
"""
import logging

from flask import (abort, flash, redirect, render_template, request, url_for)
from flask_login import current_user, login_required
from sqlalchemy import func

from ..extensions import db, limiter
from ..models import (FREE_COMMENTS_PER_POST, FREE_POSTS_PER_CATEGORY,
                      ForumCategory, ForumComment, ForumCommentLike, ForumPost,
                      ForumPostLike, ForumTag)
from ..services.moderation import contains_profanity, register_violation
from . import bp

log = logging.getLogger(__name__)

BANNED_NOTICE = ("Posting is paused for your account after repeated unkind "
                 "language. You can still read the community.")
JOIN_NOTICE = ("Joining in is a member perk. A Healing or Creator membership "
               "lets you post, reply and read the whole community.")


def _can_participate() -> bool:
    """True if the current user may post/comment/like (Healing+ or owner)."""
    return bool(getattr(current_user, "is_authenticated", False)
                and current_user.is_member())


def _require_member(redirect_to):
    """Flash + redirect if the current user can't participate; else None."""
    if not _can_participate():
        flash(JOIN_NOTICE, "info")
        return redirect(redirect_to)
    return None


def _anon_default():
    return bool(getattr(current_user, "default_anonymous", False))


def _wants_anonymous():
    return request.form.get("anonymous") == "1"


def _guard_content(*texts) -> bool:
    """Returns True if the content is clean; otherwise warns/bans and flashes."""
    if any(contains_profanity(t) for t in texts if t):
        status = register_violation(current_user)
        flash(status["message"], "error")
        return False
    return True


@bp.route("/")
def index():
    cats = ForumCategory.query.order_by(ForumCategory.sort_order, ForumCategory.name).all()
    counts = dict(
        db.session.query(ForumPost.category_id, func.count(ForumPost.id))
        .filter(ForumPost.hidden.is_(False)).group_by(ForumPost.category_id).all()
    )
    return render_template("forums/index.html", categories=cats, counts=counts)


@bp.route("/c/<slug>")
def category(slug):
    cat = ForumCategory.query.filter_by(slug=slug).first_or_404()
    tags = cat.tags.all()
    active_tag = None
    tag_slug = request.args.get("tag")
    if tag_slug:
        active_tag = next((t for t in tags if t.slug == tag_slug), None)

    query = cat.posts.filter_by(hidden=False)
    if active_tag:
        query = query.filter_by(tag_id=active_tag.id)
    query = query.order_by(ForumPost.created_at.desc())

    can_participate = _can_participate()
    limited = not can_participate
    if limited:
        posts = query.limit(FREE_POSTS_PER_CATEGORY).all()
    else:
        posts = query.limit(100).all()

    view = "list" if request.args.get("view") == "list" else "tiles"
    return render_template("forums/category.html", category=cat, posts=posts,
                           tags=tags, active_tag=active_tag,
                           anon_default=_anon_default(), view=view,
                           can_participate=can_participate, limited=limited)


@bp.route("/c/<slug>/new", methods=["POST"])
@login_required
@limiter.limit("15 per hour")
def create_post(slug):
    cat = ForumCategory.query.filter_by(slug=slug).first_or_404()
    blocked = _require_member(url_for("forums.category", slug=slug))
    if blocked:
        return blocked
    if current_user.forum_banned:
        flash(BANNED_NOTICE, "error")
        return redirect(url_for("forums.category", slug=slug))

    title = (request.form.get("title") or "").strip()[:160]
    body = (request.form.get("body") or "").strip()[:8000]
    if not title or not body:
        flash("A post needs a title and a few words.", "error")
        return redirect(url_for("forums.category", slug=slug))

    tag_id = None
    raw_tag = request.form.get("tag_id")
    if raw_tag and raw_tag.isdigit():
        tag = db.session.get(ForumTag, int(raw_tag))
        if tag and tag.category_id == cat.id:
            tag_id = tag.id

    if not _guard_content(title, body):
        return redirect(url_for("forums.category", slug=slug))

    post = ForumPost(category_id=cat.id, tag_id=tag_id, user_id=current_user.id,
                     title=title, body=body, anonymous=_wants_anonymous())
    db.session.add(post)
    db.session.commit()
    flash("Posted. Thank you for adding your voice.", "success")
    return redirect(url_for("forums.post", post_id=post.id))


@bp.route("/p/<int:post_id>")
def post(post_id):
    post = db.session.get(ForumPost, post_id)
    if post is None or post.hidden:
        abort(404)
    can_participate = _can_participate()
    # top-level comments, each with its (one-level) replies
    top = (post.comments.filter_by(hidden=False, parent_id=None)
           .order_by(ForumComment.created_at).all())
    total_top = len(top)
    limited = not can_participate and total_top > FREE_COMMENTS_PER_POST
    if not can_participate:
        top = top[:FREE_COMMENTS_PER_POST]
    all_ids = []
    threads = []
    for c in top:
        replies = [r for r in c.replies if not r.hidden]
        threads.append((c, replies))
        all_ids.append(c.id)
        all_ids.extend(r.id for r in replies)
    liked_posts, liked_comments = _liked_ids([post_id], all_ids)
    return render_template("forums/post.html", post=post, threads=threads,
                           comment_count=len(all_ids),
                           liked_posts=liked_posts, liked_comments=liked_comments,
                           anon_default=_anon_default(),
                           can_participate=can_participate, limited=limited)


@bp.route("/p/<int:post_id>/comment", methods=["POST"])
@login_required
@limiter.limit("30 per hour")
def create_comment(post_id):
    post = db.session.get(ForumPost, post_id)
    if post is None or post.hidden:
        abort(404)
    blocked = _require_member(url_for("forums.post", post_id=post_id))
    if blocked:
        return blocked
    if current_user.forum_banned:
        flash(BANNED_NOTICE, "error")
        return redirect(url_for("forums.post", post_id=post_id))

    body = (request.form.get("body") or "").strip()[:4000]
    if not body:
        flash("Write a little something first.", "error")
        return redirect(url_for("forums.post", post_id=post_id))

    # optional reply target — flattened so threads never nest deeper than one level
    parent_id = None
    raw_parent = request.form.get("parent_id")
    if raw_parent and raw_parent.isdigit():
        parent = db.session.get(ForumComment, int(raw_parent))
        if parent and parent.post_id == post.id and not parent.hidden:
            parent_id = parent.parent_id or parent.id

    if not _guard_content(body):
        return redirect(url_for("forums.post", post_id=post_id))

    db.session.add(ForumComment(post_id=post.id, parent_id=parent_id,
                                user_id=current_user.id, body=body,
                                anonymous=_wants_anonymous()))
    db.session.commit()
    return redirect(url_for("forums.post", post_id=post_id) + "#comments")


@bp.route("/p/<int:post_id>/like", methods=["POST"])
@login_required
def like_post(post_id):
    post = db.session.get(ForumPost, post_id)
    if post is None or post.hidden:
        abort(404)
    blocked = _require_member(url_for("forums.post", post_id=post_id))
    if blocked:
        return blocked
    existing = ForumPostLike.query.filter_by(user_id=current_user.id, post_id=post.id).first()
    if existing:
        db.session.delete(existing)
    else:
        db.session.add(ForumPostLike(user_id=current_user.id, post_id=post.id))
    db.session.commit()
    return redirect(request.form.get("next") or url_for("forums.post", post_id=post_id))


@bp.route("/comment/<int:comment_id>/like", methods=["POST"])
@login_required
def like_comment(comment_id):
    comment = db.session.get(ForumComment, comment_id)
    if comment is None or comment.hidden:
        abort(404)
    blocked = _require_member(url_for("forums.post", post_id=comment.post_id))
    if blocked:
        return blocked
    existing = ForumCommentLike.query.filter_by(
        user_id=current_user.id, comment_id=comment.id).first()
    if existing:
        db.session.delete(existing)
    else:
        db.session.add(ForumCommentLike(user_id=current_user.id, comment_id=comment.id))
    db.session.commit()
    return redirect(request.form.get("next")
                    or url_for("forums.post", post_id=comment.post_id) + "#comments")


def _liked_ids(post_ids, comment_ids):
    """Sets of post/comment ids the current member has liked (empty if anon)."""
    if not current_user.is_authenticated:
        return set(), set()
    liked_posts = set()
    liked_comments = set()
    if post_ids:
        liked_posts = {r.post_id for r in ForumPostLike.query.filter(
            ForumPostLike.user_id == current_user.id,
            ForumPostLike.post_id.in_(post_ids)).all()}
    if comment_ids:
        liked_comments = {r.comment_id for r in ForumCommentLike.query.filter(
            ForumCommentLike.user_id == current_user.id,
            ForumCommentLike.comment_id.in_(comment_ids)).all()}
    return liked_posts, liked_comments
