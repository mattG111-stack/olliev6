from datetime import datetime, timezone
import pytest
from models import ImportBatch, PropertyForSale, User, VerificationCode
import release


def batch(db, status='staged', rows=0, kind='for_sale', active=False):
    b = ImportBatch(batch_type=kind, region='Auckland', filename='launch.csv',
                    status=status, rows_inserted=rows, rows_total=rows, is_active=active)
    db.add(b); db.flush()
    return b


def listing(db, b, slug, dead=False, held=False):
    p = PropertyForSale(import_batch_id=b.id, slug_id=slug, address=slug,
                        region='Auckland', asking_price=1_000_000,
                        fair_value=1_100_000, floor_area_m2=120, property_type='House',
                        is_held=held, link_dead_at=datetime.now(timezone.utc) if dead else None)
    db.add(p); db.flush()
    return p


def test_repeated_publish_cannot_activate_empty_duplicate(db_session):
    real = batch(db_session, rows=1)
    listing(db_session, real, 'live-home')
    empty = batch(db_session)
    db_session.commit()
    release.send_to_preview(db_session)
    release.publish_release(db_session)
    assert release.publish_release(db_session)['count'] == 0
    assert real.is_active
    assert not empty.is_active
    assert release.staged_summary(db_session).has_staged is False


def test_repeated_publish_cannot_revert_to_older_working_snapshot(db_session):
    old = batch(db_session, rows=10)
    new = batch(db_session, rows=2)
    db_session.commit()
    release.publish_release(db_session)
    assert release.publish_release(db_session)['count'] == 0
    assert new.is_active and not old.is_active
    assert old.status == 'superseded'


def test_sold_working_history_remains_readable_after_publish(db_session):
    old = batch(db_session, rows=10, kind='sold')
    new = batch(db_session, rows=2, kind='sold')
    db_session.commit()
    release.publish_release(db_session)
    assert old.status == new.status == 'published'
    assert release.publish_release(db_session)['count'] == 0


def test_restore_does_not_resurrect_newer_dead_listing(db_session):
    old = batch(db_session, status='archived', rows=1)
    listing(db_session, old, 'sold-home')
    recent = batch(db_session, status='archived', rows=1)
    listing(db_session, recent, 'sold-home', dead=True)
    live = batch(db_session, status='published', active=True)
    db_session.commit()
    assert release.restorable_count(db_session)['restorable'] == 0
    assert release.restore_missing_listings(db_session)['restored'] == 0
    assert db_session.query(PropertyForSale).filter_by(import_batch_id=live.id).count() == 0


def test_restore_latest_live_copy_and_repeat_are_consistent(db_session):
    old = batch(db_session, status='archived', rows=1)
    listing(db_session, old, 'relisted', dead=True)
    recent = batch(db_session, status='archived', rows=1)
    listing(db_session, recent, 'relisted')
    batch(db_session, status='published', active=True)
    db_session.commit()
    assert release.restorable_count(db_session)['restorable'] == 1
    assert release.restore_missing_listings(db_session)['restored'] == 1
    assert release.restore_missing_listings(db_session)['restored'] == 0


def test_release_response_keeps_dashboard_counts(db_session):
    from routers.release import get_staged
    live = batch(db_session, status='published', active=True)
    listing(db_session, live, 'visible')
    listing(db_session, live, 'dead-and-held', dead=True, held=True)
    listing(db_session, live, 'held', held=True)
    db_session.commit()
    out = get_staged(db=db_session).model_dump()
    assert out['live_total'] == 3
    assert out['live_visible'] == 1
    assert out['live_gone'] == 1
    assert out['live_held'] == 1
    assert out['live_other_hidden'] == 0
    assert 'priced_rows' in out and 'gone_weekly' in out


def test_unsigned_webhook_fails_closed(db_session, monkeypatch):
    import checkout
    monkeypatch.setattr(checkout.settings, 'stripe_webhook_secret', '')
    with pytest.raises(checkout.CheckoutError, match='STRIPE_WEBHOOK_SECRET'):
        checkout.handle_webhook(db_session, b'{"type":"customer.subscription.updated"}', None)


def test_missing_signature_is_rejected_before_processing(db_session, monkeypatch):
    import checkout
    monkeypatch.setattr(checkout.settings, 'stripe_webhook_secret', 'test-signing-key')
    with pytest.raises(ValueError, match='signature'):
        checkout.handle_webhook(db_session, b'{}', None)


@pytest.mark.parametrize('email,password', [('', ''), ('admin@example.com', 'changeme')])
def test_default_admin_is_not_created(db_session, monkeypatch, email, password):
    import security
    monkeypatch.setattr(security.settings, 'seed_admin_email', email)
    monkeypatch.setattr(security.settings, 'seed_admin_password', password)
    security.ensure_seed_admin(db_session)
    assert db_session.query(User).count() == 0


def test_verification_accepts_sqlite_timestamp(db_session):
    import verification
    user = User(email='verify@example.test', password_hash='unused', role='user', status='pending')
    db_session.add(user); db_session.commit()
    code = verification.issue_code(db_session, user.id, 'email')
    assert verification.check_code(db_session, user.id, 'email', code) == (True, 'ok')
    assert verification.check_code(db_session, user.id, 'email', code) == (False, 'no_code')


def test_delete_live_restores_published_status_and_keeps_rows_out_of_staged(db_session):
    from routers.admin_upload import delete_batch
    old = batch(db_session, status='archived')
    current = batch(db_session, status='published', rows=1, active=True)
    listing(db_session, current, 'keep-home')
    unreviewed = batch(db_session, rows=1)
    db_session.commit()
    out = delete_batch(current.id, None, db_session)
    assert out.now_active_batch_id == old.id
    assert out.kept_moved_to_batch_id == old.id
    db_session.refresh(old)
    assert old.status == 'published' and old.is_active
    assert not unreviewed.is_active
    assert db_session.query(PropertyForSale).filter_by(import_batch_id=old.id).count() == 1


def test_grid_enrichment_and_publish_select_same_nonempty_batch(db_session):
    from staged_stages import _staged_forsale_batch, enrichable_forsale_batch
    from routers.release import staged_rows
    real = batch(db_session, rows=1)
    listing(db_session, real, 'review-me')
    batch(db_session)
    db_session.commit()
    assert _staged_forsale_batch(db_session, 'Auckland').id == real.id
    assert enrichable_forsale_batch(db_session, 'Auckland').id == real.id
    assert staged_rows(db=db_session).total == 1


def test_shared_working_statuses_remain_importable():
    from release import WORKING_STATUSES
    import staged_stages
    import portals.listings
    assert WORKING_STATUSES == ('staged', 'preview')
    assert staged_stages.WORKING_STATUSES == WORKING_STATUSES


def test_customer_changes_do_not_compare_against_unpublished_uploads(db_session):
    from routers.dashboards import _previous_batch_id
    old = batch(db_session, status='archived', rows=3)
    live = batch(db_session, status='published', active=True, rows=3)
    batch(db_session, rows=1)
    db_session.commit()
    assert _previous_batch_id(db_session, 'for_sale', 'Auckland', live.id) == old.id


def test_portal_approval_targets_reviewed_data_not_empty_reupload(db_session):
    from portals.listings import target_batch
    real = batch(db_session, rows=1)
    batch(db_session)
    db_session.commit()
    assert target_batch(db_session).id == real.id


def test_suppressed_margin_cannot_become_a_dollar_sort_signal(db_session):
    from routers.properties import _sort_expression
    b = batch(db_session, status='published', active=True)
    withheld = listing(db_session, b, 'withheld')
    endorsed = listing(db_session, b, 'endorsed')
    endorsed.margin = 0.1
    db_session.commit()
    rows = dict(db_session.query(PropertyForSale.id, _sort_expression('margin_dollars')).all())
    assert rows[withheld.id] is None
    assert rows[endorsed.id] == 100_000


def test_deleting_unreviewed_batch_does_not_publish_its_listings(db_session):
    from routers.admin_upload import delete_batch
    live = batch(db_session, status='published', active=True)
    draft = batch(db_session, rows=1)
    listing(db_session, draft, 'unreviewed')
    db_session.commit()
    result = delete_batch(draft.id, None, db_session)
    assert result.rows_deleted == 1
    assert result.rows_kept == 0
    assert db_session.query(PropertyForSale).filter_by(import_batch_id=live.id).count() == 0


def test_publish_can_roll_back_with_its_audit_record(db_session):
    old = batch(db_session, status='published', active=True)
    new = batch(db_session, rows=1)
    db_session.commit()
    release.publish_release(db_session, commit=False)
    db_session.rollback()
    db_session.refresh(old); db_session.refresh(new)
    assert old.is_active and old.status == 'published'
    assert not new.is_active and new.status == 'staged'
