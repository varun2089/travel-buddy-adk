from sqlalchemy import select

from .models import AuditRecord, Proposal, Review


def add_proposal(session, inputs, results, request, normalized_inputs):
    proposal = Proposal(inputs=inputs, results=results, request_scope=request.scope,
                        request_key=request.key, normalized_inputs=normalized_inputs)
    session.add(proposal)
    session.flush()
    review = Review(proposal_id=proposal.id)
    session.add(review)
    session.flush()
    return proposal, review


def get_proposal(session, proposal_id, lock=False):
    statement = select(Review).where(Review.proposal_id == proposal_id)
    if lock:
        statement = statement.with_for_update()
    review = session.scalar(statement)
    if review is None:
        raise ValueError("Proposal not found.")
    return session.get(Proposal, proposal_id), review


def add_audit(session, proposal_id, reviewer, action, confirmation):
    session.add(AuditRecord(
        proposal_id=proposal_id, reviewer=reviewer,
        action=action, confirmation=confirmation,
    ))


def find_proposal_request(session, request):
    row = session.execute(select(Proposal, Review).join(Review).where(
        Proposal.request_scope == request.scope, Proposal.request_key == request.key
    )).first()
    return row
