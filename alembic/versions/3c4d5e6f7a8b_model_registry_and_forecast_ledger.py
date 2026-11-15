"""model_registry_and_forecast_ledger

Revision ID: 3c4d5e6f7a8b
Revises: 2a5b8c9d1e2f
Create Date: 2026-11-15 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3c4d5e6f7a8b'
down_revision: Union[str, Sequence[str], None] = '2a5b8c9d1e2f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Model artifact registry columns
    with op.batch_alter_table('model_artifact_records') as batch_op:
        batch_op.add_column(sa.Column('model_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('strategy', sa.String(length=64), nullable=False, server_default='machine_learning'))
        batch_op.add_column(sa.Column('status', sa.String(length=32), nullable=False, server_default='active'))
        batch_op.add_column(sa.Column('hyperparameters_json', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('package_versions_json', sa.Text(), nullable=True))
        batch_op.create_index('ix_model_artifact_model_id', ['model_id'])
        batch_op.create_index('ix_model_artifact_status', ['status'])

    # Forecast ledger columns
    with op.batch_alter_table('forecast_records') as batch_op:
        batch_op.add_column(sa.Column('lead_day', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('seasonal_naive_predicted', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('actual_demand', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('actual_observed_at_utc', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('forecast_records') as batch_op:
        batch_op.drop_column('actual_observed_at_utc')
        batch_op.drop_column('actual_demand')
        batch_op.drop_column('seasonal_naive_predicted')
        batch_op.drop_column('lead_day')

    with op.batch_alter_table('model_artifact_records') as batch_op:
        batch_op.drop_index('ix_model_artifact_status')
        batch_op.drop_index('ix_model_artifact_model_id')
        batch_op.drop_column('package_versions_json')
        batch_op.drop_column('hyperparameters_json')
        batch_op.drop_column('status')
        batch_op.drop_column('strategy')
        batch_op.drop_column('model_id')
