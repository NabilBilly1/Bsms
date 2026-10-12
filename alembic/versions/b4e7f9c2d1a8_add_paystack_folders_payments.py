"""add_paystack_folders_payments

Revision ID: b4e7f9c2d1a8
Revises: 33429ad950d3
Create Date: 2026-10-12 15:42:00.000000

Adds:
  - companies.paystack_public_key
  - companies.paystack_secret_key
  - contact_folders table
  - folder_customers join table
  - system_settings table
  - payment_transactions table
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4e7f9c2d1a8'
down_revision: Union[str, Sequence[str], None] = '33429ad950d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    # ── 1. Add Paystack columns to companies ──────────────────────────────────
    op.add_column('companies', sa.Column('paystack_public_key', sa.String(), nullable=True))
    op.add_column('companies', sa.Column('paystack_secret_key', sa.String(), nullable=True))

    # ── 2. contact_folders ────────────────────────────────────────────────────
    op.create_table(
        'contact_folders',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('branch_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['branch_id'], ['branches.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_contact_folders_id'), 'contact_folders', ['id'], unique=False)
    op.create_index(op.f('ix_contact_folders_name'), 'contact_folders', ['name'], unique=False)
    op.create_index(op.f('ix_contact_folders_branch_id'), 'contact_folders', ['branch_id'], unique=False)

    # ── 3. folder_customers join table ────────────────────────────────────────
    op.create_table(
        'folder_customers',
        sa.Column('folder_id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['folder_id'], ['contact_folders.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('folder_id', 'customer_id'),
    )

    # ── 4. system_settings ───────────────────────────────────────────────────
    op.create_table(
        'system_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('value', sa.Text(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_system_settings_id'), 'system_settings', ['id'], unique=False)
    op.create_index(op.f('ix_system_settings_key'), 'system_settings', ['key'], unique=True)

    # ── 5. payment_transactions ───────────────────────────────────────────────
    op.create_table(
        'payment_transactions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('reference', sa.String(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=True),
        sa.Column('branch_id', sa.Integer(), nullable=True),
        sa.Column('amount', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(), nullable=True),
        sa.Column('customer_email', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=True),
        sa.Column('channel', sa.String(), nullable=True),
        sa.Column('purpose', sa.String(), nullable=True),
        sa.Column('credits_purchased', sa.Integer(), nullable=True),
        sa.Column('paystack_response', sa.Text(), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['branch_id'], ['branches.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_payment_transactions_id'), 'payment_transactions', ['id'], unique=False)
    op.create_index(op.f('ix_payment_transactions_reference'), 'payment_transactions', ['reference'], unique=True)
    op.create_index(op.f('ix_payment_transactions_customer_email'), 'payment_transactions', ['customer_email'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""

    # Drop in reverse order of creation
    op.drop_index(op.f('ix_payment_transactions_customer_email'), table_name='payment_transactions')
    op.drop_index(op.f('ix_payment_transactions_reference'), table_name='payment_transactions')
    op.drop_index(op.f('ix_payment_transactions_id'), table_name='payment_transactions')
    op.drop_table('payment_transactions')

    op.drop_index(op.f('ix_system_settings_key'), table_name='system_settings')
    op.drop_index(op.f('ix_system_settings_id'), table_name='system_settings')
    op.drop_table('system_settings')

    op.drop_table('folder_customers')

    op.drop_index(op.f('ix_contact_folders_branch_id'), table_name='contact_folders')
    op.drop_index(op.f('ix_contact_folders_name'), table_name='contact_folders')
    op.drop_index(op.f('ix_contact_folders_id'), table_name='contact_folders')
    op.drop_table('contact_folders')

    op.drop_column('companies', 'paystack_secret_key')
    op.drop_column('companies', 'paystack_public_key')
