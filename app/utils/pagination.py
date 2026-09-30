from .validation import query_int


def paginate(query, default_per_page=20, max_per_page=100):
    page = query_int("page", 1, min_value=1)
    per_page = query_int("per_page", default_per_page, min_value=1, max_value=max_per_page)
    total = query.order_by(None).count()
    items = query.limit(per_page).offset((page - 1) * per_page).all()
    pages = (total + per_page - 1) // per_page if total else 0
    return items, {
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": pages,
        "has_next": page < pages,
        "has_prev": page > 1,
    }
