"""enterprise_entities

Revision ID: 2a5b8c9d1e2f
Revises: 13027f25dc62
Create Date: 2026-11-13 21:38:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2a5b8c9d1e2f'
down_revision: Union[str, Sequence[str], None] = '13027f25dc62'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('agencies',
        sa.Column('agency_id', sa.String(length=64), primary_key=True),
        sa.Column('agency_name', sa.String(length=255), nullable=False),
        sa.Column('timezone', sa.String(length=64), nullable=False, server_default='UTC'),
        sa.Column('contact_email', sa.String(length=255), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('created_at_utc', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at_utc', sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table('user_accounts',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('username', sa.String(length=80), unique=True, nullable=False),
        sa.Column('email', sa.String(length=255), unique=True, nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=True),
        sa.Column('role', sa.String(length=32), nullable=False, server_default='analyst'),
        sa.Column('agency_id', sa.String(length=64), nullable=False, server_default='default'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('created_at_utc', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_login_utc', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_user_accounts_agency_id', 'user_accounts', ['agency_id'])
    op.create_index('ix_user_accounts_username', 'user_accounts', ['username'])

    op.create_table('vehicles',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('agency_id', sa.String(length=64), nullable=False, server_default='default'),
        sa.Column('vehicle_id', sa.String(length=64), nullable=False),
        sa.Column('label', sa.String(length=128), nullable=True),
        sa.Column('model', sa.String(length=128), nullable=True),
        sa.Column('seated_capacity', sa.Integer(), nullable=False, server_default='40'),
        sa.Column('standing_capacity', sa.Integer(), nullable=False, server_default='15'),
        sa.Column('total_capacity', sa.Integer(), nullable=False, server_default='55'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('created_at_utc', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('agency_id', 'vehicle_id', name='uq_vehicle_agency_id'),
    )
    op.create_index('ix_vehicles_agency_id', 'vehicles', ['agency_id'])
    op.create_index('ix_vehicles_vehicle_id', 'vehicles', ['vehicle_id'])

    op.create_table('trip_passenger_observations',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('agency_id', sa.String(length=64), nullable=False, server_default='default'),
        sa.Column('trip_id', sa.String(length=128), nullable=False),
        sa.Column('route_id', sa.String(length=128), nullable=False),
        sa.Column('vehicle_id', sa.String(length=64), nullable=True),
        sa.Column('service_date', sa.Date(), nullable=False),
        sa.Column('start_time_utc', sa.DateTime(timezone=True), nullable=True),
        sa.Column('end_time_utc', sa.DateTime(timezone=True), nullable=True),
        sa.Column('total_boardings', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('total_alightings', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('source_system', sa.String(length=64), nullable=False, server_default='apc'),
        sa.Column('created_at_utc', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('agency_id', 'trip_id', 'service_date', 'source_system', name='uq_trip_obs_agency_trip_date'),
    )
    op.create_index('ix_trip_obs_agency_id', 'trip_passenger_observations', ['agency_id'])
    op.create_index('ix_trip_obs_trip_id', 'trip_passenger_observations', ['trip_id'])
    op.create_index('ix_trip_obs_route_date', 'trip_passenger_observations', ['agency_id', 'route_id', 'service_date'])

    op.create_table('stop_passenger_observations',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('agency_id', sa.String(length=64), nullable=False, server_default='default'),
        sa.Column('route_id', sa.String(length=128), nullable=False),
        sa.Column('trip_id', sa.String(length=128), nullable=False),
        sa.Column('stop_id', sa.String(length=128), nullable=False),
        sa.Column('stop_sequence', sa.Integer(), nullable=False),
        sa.Column('observation_timestamp_utc', sa.DateTime(timezone=True), nullable=False),
        sa.Column('boardings', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('alightings', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('onboard_occupancy', sa.Integer(), nullable=True),
        sa.Column('vehicle_id', sa.String(length=64), nullable=True),
        sa.Column('source_system', sa.String(length=64), nullable=False, server_default='apc'),
        sa.Column('created_at_utc', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_stop_obs_trip_seq', 'stop_passenger_observations', ['agency_id', 'trip_id', 'stop_sequence'])
    op.create_index('ix_stop_obs_stop_time', 'stop_passenger_observations', ['agency_id', 'stop_id', 'observation_timestamp_utc'])

    op.create_table('forecast_records',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('agency_id', sa.String(length=64), nullable=False, server_default='default'),
        sa.Column('route_id', sa.String(length=128), nullable=False),
        sa.Column('forecast_date', sa.Date(), nullable=False),
        sa.Column('forecast_horizon_days', sa.Integer(), nullable=False),
        sa.Column('predicted_demand', sa.Float(), nullable=False),
        sa.Column('lower_bound', sa.Float(), nullable=True),
        sa.Column('upper_bound', sa.Float(), nullable=True),
        sa.Column('model_version', sa.String(length=64), nullable=False),
        sa.Column('created_at_utc', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_forecast_agency_route_date', 'forecast_records', ['agency_id', 'route_id', 'forecast_date'])

    op.create_table('operational_alert_records',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('agency_id', sa.String(length=64), nullable=False, server_default='default'),
        sa.Column('alert_id', sa.String(length=64), unique=True, nullable=False),
        sa.Column('alert_type', sa.String(length=64), nullable=False),
        sa.Column('severity', sa.String(length=32), nullable=False),
        sa.Column('route_id', sa.String(length=128), nullable=True),
        sa.Column('trip_id', sa.String(length=128), nullable=True),
        sa.Column('vehicle_id', sa.String(length=64), nullable=True),
        sa.Column('summary', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_at_utc', sa.DateTime(timezone=True), nullable=False),
        sa.Column('acknowledged', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('acknowledged_by', sa.String(length=80), nullable=True),
        sa.Column('acknowledged_at_utc', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resolved', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('resolved_at_utc', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_op_alert_agency_id', 'operational_alert_records', ['agency_id'])
    op.create_index('ix_op_alert_created', 'operational_alert_records', ['created_at_utc'])
    op.create_index('ix_op_alert_type', 'operational_alert_records', ['alert_type'])
    op.create_index('ix_op_alert_severity', 'operational_alert_records', ['severity'])

    op.create_table('data_sources',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('agency_id', sa.String(length=64), nullable=False, server_default='default'),
        sa.Column('source_name', sa.String(length=128), nullable=False),
        sa.Column('source_type', sa.String(length=64), nullable=False),
        sa.Column('feed_url', sa.String(length=512), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('created_at_utc', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_data_sources_agency_id', 'data_sources', ['agency_id'])

    op.create_table('import_jobs',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('agency_id', sa.String(length=64), nullable=False, server_default='default'),
        sa.Column('job_id', sa.String(length=64), unique=True, nullable=False),
        sa.Column('job_type', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='PENDING'),
        sa.Column('source_filename', sa.String(length=255), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('records_processed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('records_inserted', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('records_updated', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('started_at_utc', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at_utc', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_import_jobs_agency_id', 'import_jobs', ['agency_id'])
    op.create_index('ix_import_jobs_status', 'import_jobs', ['status'])


def downgrade() -> None:
    op.drop_table('import_jobs')
    op.drop_table('data_sources')
    op.drop_table('operational_alert_records')
    op.drop_table('forecast_records')
    op.drop_table('stop_passenger_observations')
    op.drop_table('trip_passenger_observations')
    op.drop_table('vehicles')
    op.drop_table('user_accounts')
    op.drop_table('agencies')
