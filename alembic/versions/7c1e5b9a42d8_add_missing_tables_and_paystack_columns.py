"""add_missing_tables_and_paystack_columns

Revision ID: 7c1e5b9a42d8
Revises: 33429ad950d3
Create Date: 2026-06-06 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c1e5b9a42d8'
down_revision: Union[str, Sequence[str], None] = '33429ad950d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return any(ix['name'] == index_name for ix in inspector.get_indexes(table_name))


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return any(col['name'] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    """Upgrade schema.

    Idempotent: the production database may already contain some of these
    objects (e.g. created by a previous ``create_all``), so every create/add
    is guarded by an inspector check.
    """
    inspector = sa.inspect(op.get_bind())

    # --- system_settings ---
    if not inspector.has_table('system_settings'):
        op.create_table(
            'system_settings',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('key', sa.String(), nullable=False),
            sa.Column('value', sa.Text(), nullable=True),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
            sa.PrimaryKeyConstraint('id'),
        )
    inspector = sa.inspect(op.get_bind())
    if not _has_index(inspector, 'system_settings', op.f('ix_system_settings_id')):
        op.create_index(op.f('ix_system_settings_id'), 'system_settings', ['id'], unique=False)
    if not _has_index(inspector, 'system_settings', op.f('ix_system_settings_key')):
        op.create_index(op.f('ix_system_settings_key'), 'system_settings', ['key'], unique=True)

    # --- contact_folders ---
    if not inspector.has_table('contact_folders'):
        op.create_table(
            'contact_folders',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('branch_id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
            sa.ForeignKeyConstraint(['branch_id'], ['branches.id']),
            sa.PrimaryKeyConstraint('id'),
        )
    inspector = sa.inspect(op.get_bind())
    if not _has_index(inspector, 'contact_folders', op.f('ix_contact_folders_id')):
        op.create_index(op.f('ix_contact_folders_id'), 'contact_folders', ['id'], unique=False)
    if not _has_index(inspector, 'contact_folders', op.f('ix_contact_folders_branch_id')):
        op.create_index(op.f('ix_contact_folders_branch_id'), 'contact_folders', ['branch_id'], unique=False)
    if not _has_index(inspector, 'contact_folders', op.f('ix_contact_folders_name')):
        op.create_index(op.f('ix_contact_folders_name'), 'contact_folders', ['name'], unique=False)

    # --- folder_customers (association table; depends on contact_folders and customers) ---
    if not inspector.has_table('folder_customers'):
        op.create_table(
            'folder_customers',
            sa.Column('folder_id', sa.Integer(), nullable=False),
            sa.Column('customer_id', sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['folder_id'], ['contact_folders.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('folder_id', 'customer_id'),
        )

    # --- payment_transactions ---
    if not inspector.has_table('payment_transactions'):
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
            sa.ForeignKeyConstraint(['branch_id'], ['branches.id'], ondelete='SET NULL'),
            sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='SET NULL'),
            sa.PrimaryKeyConstraint('id'),
        )
    inspector = sa.inspect(op.get_bind())
    if not _has_index(inspector, 'payment_transactions', op.f('ix_payment_transactions_id')):
        op.create_index(op.f('ix_payment_transactions_id'), 'payment_transactions', ['id'], unique=False)
    if not _has_index(inspector, 'payment_transactions', op.f('ix_payment_transactions_reference')):
        op.create_index(op.f('ix_payment_transactions_reference'), 'payment_transactions', ['reference'], unique=True)
    if not _has_index(inspector, 'payment_transactions', op.f('ix_payment_transactions_customer_email')):
        op.create_index(op.f('ix_payment_transactions_customer_email'), 'payment_transactions', ['customer_email'], unique=False)

    # --- companies.paystack_* columns ---
    if not _has_column(inspector, 'companies', 'paystack_public_key'):
        op.add_column('companies', sa.Column('paystack_public_key', sa.String(), nullable=True))
    if not _has_column(inspector, 'companies', 'paystack_secret_key'):
        op.add_column('companies', sa.Column('paystack_secret_key', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    inspector = sa.inspect(op.get_bind())

    # --- companies.paystack_* columns ---
    if _has_column(inspector, 'companies', 'paystack_secret_key'):
        op.drop_column('companies', 'paystack_secret_key')
    if _has_column(inspector, 'companies', 'paystack_public_key'):
        op.drop_column('companies', 'paystack_public_key')

    # --- payment_transactions ---
    if inspector.has_table('payment_transactions'):
        for index_name in (
            'ix_payment_transactions_customer_email',
            'ix_payment_transactions_reference',
            'ix_payment_transactions_id',
        ):
            if _has_index(inspector, 'payment_transactions', index_name):
                op.drop_index(op.f(index_name), table_name='payment_transactions')
        op.drop_table('payment_transactions')

    # --- folder_customers ---
    if inspector.has_table('folder_customers'):
        op.drop_table('folder_customers')

    # --- contact_folders ---
    if inspector.has_table('contact_folders'):
        for index_name in (
            'ix_contact_folders_name',
            'ix_contact_folders_branch_id',
            'ix_contact_folders_id',
        ):
            if _has_index(inspector, 'contact_folders', index_name):
                op.drop_index(op.f(index_name), table_name='contact_folders')
        op.drop_table('contact_folders')

    # --- system_settings ---
    if inspector.has_table('system_settings'):
        for index_name in (
            'ix_system_settings_key',
            'ix_system_settings_id',
        ):
            if _has_index(inspector, 'system_settings', index_name):
                op.drop_index(op.f(index_name), table_name='system_settings')
        op.drop_table('system_settings')
