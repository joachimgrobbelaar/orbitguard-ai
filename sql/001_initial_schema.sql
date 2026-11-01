-- OrbitGuard AI: Multi-Tenant Autonomous Conjunction & Maneuver Recommendation Schema
-- Designed for PostgreSQL 15+ / Supabase with Row Level Security (RLS)

-- 1. Enable Required Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- 2. Tenants Table (University CubeSat teams, NewSpace operators, Research groups)
CREATE TABLE IF NOT EXISTS tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(100) UNIQUE NOT NULL,
    api_key_hash VARCHAR(255) NOT NULL,
    tier VARCHAR(50) DEFAULT 'academic' CHECK (tier IN ('free', 'academic', 'commercial', 'enterprise')),
    contact_email VARCHAR(255) NOT NULL,
    webhook_url TEXT,
    webhook_secret VARCHAR(255),
    telegram_chat_id VARCHAR(100),
    settings JSONB DEFAULT '{
        "conjunction_threshold_km": 10.0,
        "collision_prob_threshold": 1e-4,
        "screening_window_hours": 72,
        "auto_maneuver_calc": true,
        "auto_alert_dispatch": true
    }'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 3. Satellites Table (Primary Monitored Assets)
CREATE TABLE IF NOT EXISTS satellites (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    norad_cat_id INTEGER UNIQUE,
    name VARCHAR(150) NOT NULL,
    international_designator VARCHAR(50),
    sat_class VARCHAR(50) DEFAULT 'cubesat_3u' CHECK (sat_class IN ('cubesat_1u', 'cubesat_3u', 'cubesat_6u', 'cubesat_12u', 'microsat', 'smallsat', 'constellation_node')),
    
    -- Physical & Aerodynamic Properties
    dry_mass_kg NUMERIC(10, 3) NOT NULL DEFAULT 4.0,
    fuel_mass_kg NUMERIC(10, 3) NOT NULL DEFAULT 0.5,
    cross_section_area_m2 NUMERIC(10, 4) NOT NULL DEFAULT 0.03,
    cd_drag_coefficient NUMERIC(6, 2) NOT NULL DEFAULT 2.2,
    cr_srp_coefficient NUMERIC(6, 2) NOT NULL DEFAULT 1.2,
    hard_body_radius_m NUMERIC(8, 2) NOT NULL DEFAULT 0.5,
    
    -- Propulsion Properties
    thruster_type VARCHAR(50) DEFAULT 'cold_gas' CHECK (thruster_type IN ('none', 'cold_gas', 'chemical', 'electric', 'differential_drag')),
    thrust_n NUMERIC(10, 5) DEFAULT 0.050,  -- 50 mN
    isp_s NUMERIC(8, 2) DEFAULT 70.0,        -- 70 s Isp
    min_burn_duration_s NUMERIC(8, 2) DEFAULT 0.5,
    max_burn_duration_s NUMERIC(8, 2) DEFAULT 600.0,
    
    status VARCHAR(50) DEFAULT 'operational' CHECK (status IN ('pre_launch', 'operational', 'standby', 'decommissioned', 'reentered')),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 4. TLE Records (Ephemeris Snapshots for Primary & Secondary Objects)
CREATE TABLE IF NOT EXISTS tle_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    norad_cat_id INTEGER NOT NULL,
    satellite_name VARCHAR(150) NOT NULL,
    line1 VARCHAR(70) NOT NULL,
    line2 VARCHAR(70) NOT NULL,
    epoch_timestamp TIMESTAMPTZ NOT NULL,
    bstar NUMERIC(16, 8) NOT NULL DEFAULT 0.0,
    inclination_deg NUMERIC(10, 4) NOT NULL,
    raan_deg NUMERIC(10, 4) NOT NULL,
    eccentricity NUMERIC(12, 8) NOT NULL,
    arg_perigee_deg NUMERIC(10, 4) NOT NULL,
    mean_anomaly_deg NUMERIC(10, 4) NOT NULL,
    mean_motion_rev_day NUMERIC(14, 8) NOT NULL,
    revolution_number INTEGER NOT NULL DEFAULT 0,
    source VARCHAR(50) DEFAULT 'celestrak' CHECK (source IN ('celestrak', 'spacetrack', 'user_telemetry', 'gps_fit')),
    ingested_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for instant TLE epoch lookups
CREATE INDEX IF NOT EXISTS idx_tle_norad_epoch ON tle_records(norad_cat_id, epoch_timestamp DESC);

-- 5. Debris & Resident Space Objects Catalog (Secondary Threat Pool)
CREATE TABLE IF NOT EXISTS debris_catalog (
    norad_cat_id INTEGER PRIMARY KEY,
    name VARCHAR(150) NOT NULL,
    rcs_size VARCHAR(20) DEFAULT 'MEDIUM' CHECK (rcs_size IN ('SMALL', 'MEDIUM', 'LARGE')),
    hard_body_radius_m NUMERIC(8, 2) NOT NULL DEFAULT 1.0,
    country_code VARCHAR(10),
    launch_year INTEGER,
    decay_date DATE,
    status VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active', 'decayed', 'unknown')),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 6. Conjunction Screenings (Close Approach Events over 72h Horizons)
CREATE TABLE IF NOT EXISTS conjunction_screenings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    primary_sat_id UUID NOT NULL REFERENCES satellites(id) ON DELETE CASCADE,
    secondary_norad_id INTEGER NOT NULL,
    secondary_object_name VARCHAR(150) NOT NULL,
    
    -- Screening Time Window
    screening_window_start TIMESTAMPTZ NOT NULL,
    screening_window_end TIMESTAMPTZ NOT NULL,
    screening_window_hours INTEGER NOT NULL DEFAULT 72,
    propagation_method VARCHAR(50) DEFAULT 'J2_drag_numerical' CHECK (propagation_method IN ('J2_drag_numerical', 'pinn_symplectic', 'sgp4_fast')),
    
    -- Encounter Geometry at Time of Closest Approach (TCA)
    tca_epoch TIMESTAMPTZ NOT NULL,
    miss_distance_km NUMERIC(10, 4) NOT NULL,
    radial_miss_km NUMERIC(10, 4),
    in_track_miss_km NUMERIC(10, 4),
    cross_track_miss_km NUMERIC(10, 4),
    relative_speed_km_s NUMERIC(10, 4) NOT NULL,
    
    -- Risk Probability
    combined_hard_body_radius_m NUMERIC(8, 2) NOT NULL DEFAULT 1.5,
    collision_probability NUMERIC(16, 12) NOT NULL DEFAULT 0.0,
    risk_tier VARCHAR(20) NOT NULL CHECK (risk_tier IN ('NEGLIGIBLE', 'LOW', 'MODERATE', 'HIGH', 'CRITICAL')),
    
    -- Workflow Status
    status VARCHAR(30) DEFAULT 'screened' CHECK (status IN ('screened', 'evaluating_maneuver', 'maneuver_recommended', 'mitigated', 'cleared', 'ignored')),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conjunction_tenant_tca ON conjunction_screenings(tenant_id, tca_epoch ASC);
CREATE INDEX IF NOT EXISTS idx_conjunction_primary_risk ON conjunction_screenings(primary_sat_id, risk_tier);

-- 7. Maneuver Recommendations (Autonomous Trajectory Solutions)
CREATE TABLE IF NOT EXISTS maneuver_recommendations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conjunction_id UUID NOT NULL REFERENCES conjunction_screenings(id) ON DELETE CASCADE,
    satellite_id UUID NOT NULL REFERENCES satellites(id) ON DELETE CASCADE,
    
    -- Maneuver Strategy & Timing
    maneuver_strategy VARCHAR(50) NOT NULL CHECK (maneuver_strategy IN (
        'prograde_phasing',
        'retrograde_phasing',
        'out_of_plane_normal',
        'out_of_plane_antinormal',
        'combined_optimal'
    )),
    planned_burn_epoch TIMESTAMPTZ NOT NULL,
    lead_time_hours NUMERIC(6, 2) NOT NULL, -- Time before TCA
    
    -- Delta-V Vector in VNC (Velocity, Normal, Co-Normal) Frame [m/s]
    dv_velocity_v_m_s NUMERIC(10, 5) NOT NULL DEFAULT 0.0,
    dv_normal_n_m_s NUMERIC(10, 5) NOT NULL DEFAULT 0.0,
    dv_conormal_c_m_s NUMERIC(10, 5) NOT NULL DEFAULT 0.0,
    dv_total_magnitude_m_s NUMERIC(10, 5) NOT NULL,
    
    -- Propulsion Impact
    burn_duration_s NUMERIC(10, 2) NOT NULL,
    propellant_consumed_kg NUMERIC(10, 5) NOT NULL,
    remaining_propellant_kg NUMERIC(10, 5) NOT NULL,
    remaining_delta_v_m_s NUMERIC(10, 2) NOT NULL,
    
    -- Post-Maneuver Safety Margin Verification
    post_maneuver_miss_distance_km NUMERIC(10, 4) NOT NULL,
    post_maneuver_collision_prob NUMERIC(16, 12) NOT NULL DEFAULT 0.0,
    new_perigee_altitude_km NUMERIC(10, 2) NOT NULL,
    new_apogee_altitude_km NUMERIC(10, 2) NOT NULL,
    reentry_risk_flag BOOLEAN DEFAULT FALSE,
    
    -- Executive Brief
    executive_brief_markdown TEXT NOT NULL,
    recommendation_status VARCHAR(30) DEFAULT 'recommended' CHECK (recommendation_status IN (
        'recommended', 'operator_approved', 'operator_rejected', 'uplinked', 'executed'
    )),
    operator_notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 8. Alert Dispatches Table (Notifications via Telegram / Webhooks / Email)
CREATE TABLE IF NOT EXISTS alert_dispatches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conjunction_id UUID NOT NULL REFERENCES conjunction_screenings(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    channel VARCHAR(30) NOT NULL CHECK (channel IN ('telegram', 'webhook', 'email')),
    recipient_target VARCHAR(255) NOT NULL,
    payload_json JSONB NOT NULL,
    dispatch_status VARCHAR(20) DEFAULT 'pending' CHECK (dispatch_status IN ('pending', 'delivered', 'failed', 'retrying')),
    attempts INTEGER DEFAULT 1,
    response_code INTEGER,
    error_message TEXT,
    delivered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 9. Audit Logs Table (Security & Operations History)
CREATE TABLE IF NOT EXISTS audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID REFERENCES tenants(id) ON DELETE SET NULL,
    action VARCHAR(100) NOT NULL,
    actor_id VARCHAR(100) NOT NULL DEFAULT 'orbitguard_autonomous_agent',
    entity_type VARCHAR(50) NOT NULL,
    entity_id UUID,
    details JSONB DEFAULT '{}'::jsonb,
    ip_address VARCHAR(45),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 10. Row Level Security (RLS) Configuration for Supabase
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE satellites ENABLE ROW LEVEL SECURITY;
ALTER TABLE conjunction_screenings ENABLE ROW LEVEL SECURITY;
ALTER TABLE maneuver_recommendations ENABLE ROW LEVEL SECURITY;
ALTER TABLE alert_dispatches ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;

-- Tenants Policy
CREATE POLICY tenant_isolation_tenants ON tenants
    FOR ALL
    USING (id = auth.uid()::uuid OR current_setting('request.jwt.claim.role', true) = 'service_role');

-- Satellites Policy
CREATE POLICY tenant_isolation_satellites ON satellites
    FOR ALL
    USING (tenant_id = auth.uid()::uuid OR current_setting('request.jwt.claim.role', true) = 'service_role');

-- Conjunction Screenings Policy
CREATE POLICY tenant_isolation_conjunctions ON conjunction_screenings
    FOR ALL
    USING (tenant_id = auth.uid()::uuid OR current_setting('request.jwt.claim.role', true) = 'service_role');

-- Maneuver Recommendations Policy
CREATE POLICY tenant_isolation_maneuvers ON maneuver_recommendations
    FOR ALL
    USING (satellite_id IN (SELECT id FROM satellites WHERE tenant_id = auth.uid()::uuid)
           OR current_setting('request.jwt.claim.role', true) = 'service_role');

-- Alert Dispatches Policy
CREATE POLICY tenant_isolation_alerts ON alert_dispatches
    FOR ALL
    USING (tenant_id = auth.uid()::uuid OR current_setting('request.jwt.claim.role', true) = 'service_role');

-- 11. Stored Procedures / RPC Functions for Autonomous Agent Operations

-- Function: Get Active Critical Conjunctions
CREATE OR REPLACE FUNCTION fn_get_critical_conjunctions(
    p_tenant_id UUID,
    p_horizon_hours INT DEFAULT 72
)
RETURNS TABLE (
    conjunction_id UUID,
    satellite_name VARCHAR,
    secondary_object_name VARCHAR,
    tca_epoch TIMESTAMPTZ,
    miss_distance_km NUMERIC,
    collision_probability NUMERIC,
    risk_tier VARCHAR,
    status VARCHAR
) LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    RETURN QUERY
    SELECT 
        c.id AS conjunction_id,
        s.name AS satellite_name,
        c.secondary_object_name,
        c.tca_epoch,
        c.miss_distance_km,
        c.collision_probability,
        c.risk_tier,
        c.status
    FROM conjunction_screenings c
    JOIN satellites s ON c.primary_sat_id = s.id
    WHERE c.tenant_id = p_tenant_id
      AND c.tca_epoch BETWEEN NOW() AND (NOW() + (p_horizon_hours || ' hours')::INTERVAL)
      AND c.risk_tier IN ('HIGH', 'CRITICAL')
      AND c.status NOT IN ('cleared', 'ignored')
    ORDER BY c.collision_probability DESC, c.miss_distance_km ASC;
END;
$$;
