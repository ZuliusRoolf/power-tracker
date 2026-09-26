/**
 * =============================================================================
 * Zigbee2MQTT External Converter: IKEA INSPELNING Smart Plug Failsafe
 * =============================================================================
 *
 * CRITICAL PURPOSE:
 * This smart plug powers the physical Proxmox VE host machine.
 * If this plug ever receives a turn-off command ('state: OFF' or 'TOGGLE'),
 * the Proxmox host and all hosted virtual machines/containers will suffer
 * an instant power cut, causing potential data corruption.
 *
 * FAILSAFE MECHANISMS IMPLEMENTED IN THIS CONVERTER:
 * 1. Read-Only UI Exposure:
 *    The 'state' attribute is exposed with access: ea.STATE (read-only) instead
 *    of ea.ALL or ea.STATE_SET. This completely removes the toggle switch
 *    from the Zigbee2MQTT Frontend web interface, preventing accidental clicks.
 *
 * 2. Execution Blocker in 'toZigbee':
 *    The standard 'tz.on_off' converter is intercepted. Any attempt to send
 *    'OFF' or 'TOGGLE' (via MQTT, WebSocket, REST API, or automation) triggers
 *    a critical error and is immediately aborted BEFORE any Zigbee frame is sent.
 *
 * 3. Power-On Behavior Default:
 *    Ensures the relay defaults to 'ON' whenever AC mains power is restored.
 * =============================================================================
 */

const fz = require('zigbee-herdsman-converters/converters/fromZigbee');
const tz = require('zigbee-herdsman-converters/converters/toZigbee');
const exposes = require('zigbee-herdsman-converters/lib/exposes');
const reporting = require('zigbee-herdsman-converters/lib/reporting');

const e = exposes.presets;
const ea = exposes.access;

// Failsafe toZigbee handler that strictly forbids OFF / TOGGLE commands
const failsafeOnOff = {
    key: ['state', 'on_off', 'switch'],
    convertSet: async (entity, key, value, meta) => {
        const stateStr = String(value).toUpperCase();

        if (stateStr === 'OFF' || stateStr === 'TOGGLE' || stateStr === '0' || stateStr === 'FALSE') {
            const devName = meta.options.friendly_name || meta.device.ieeeAddr || 'Proxmox Power Plug';
            const alertMsg = `[CRITICAL FAILSAFE INTERCEPT] Blocked attempt to send '${stateStr}' to '${devName}'! ` +
                             `This plug powers the Proxmox VE host and must NEVER be switched off.`;

            if (meta.logger && meta.logger.error) {
                meta.logger.error(alertMsg);
            } else {
                console.error(alertMsg);
            }

            throw new Error(alertMsg);
        }

        // Only allow explicit 'ON' commands (e.g., initial turn on or safety re-arm)
        if (stateStr === 'ON' || stateStr === '1' || stateStr === 'TRUE') {
            if (tz.on_off && tz.on_off.convertSet) {
                return await tz.on_off.convertSet(entity, key, 'ON', meta);
            }
            return {state: {state: 'ON'}};
        }

        throw new Error(`[CRITICAL FAILSAFE] Unsupported command '${value}' on Proxmox power plug.`);
    },
    convertGet: async (entity, key, meta) => {
        if (tz.on_off && tz.on_off.convertGet) {
            return await tz.on_off.convertGet(entity, key, meta);
        }
    },
};

const definition = {
    zigbeeModel: ['INSPELNING Smart plug'],
    model: 'E2206-FAILSAFE',
    vendor: 'IKEA of Sweden',
    description: 'INSPELNING Smart plug (Proxmox VE Host Power - HARDENED FAILSAFE)',
    fromZigbee: [
        fz.on_off,
        fz.electrical_measurement,
        fz.metering,
        fz.ignore_basic_report,
    ],
    // Use our failsafe handler instead of standard tz.on_off
    toZigbee: [
        failsafeOnOff,
        tz.power_on_behavior,
    ],
    exposes: [
        e.power().withDescription('Instantaneous power draw in Watts'),
        e.current().withDescription('Instantaneous current in Amperes'),
        e.voltage().withDescription('Mains line voltage in Volts'),
        e.energy().withDescription('Lifetime cumulative energy consumed in kWh'),
        // Read-only state display: ea.STATE only (no ea.SET -> NO TOGGLE IN UI)
        e.binary('state', ea.STATE, 'ON', 'OFF')
            .withDescription('Relay state (LOCKED ON - Read Only Failsafe)'),
        e.power_on_behavior(['on']),
    ],
    configure: async (device, coordinatorEndpoint, logger) => {
        const endpoint = device.getEndpoint(1);
        await reporting.bind(endpoint, coordinatorEndpoint, [
            'genOnOff',
            'haElectricalMeasurement',
            'seMetering',
        ]);
        await reporting.onOff(endpoint);
        await reporting.readEletricalMeasurementMultiplierDivisors(endpoint);
        await reporting.readMeteringMultiplierDivisor(endpoint);
        // Report active power changes >= 1 Watt, or at least every 60 seconds
        await reporting.activePower(endpoint, {min: 5, max: 60, change: 1});
        // Report energy consumption changes
        await reporting.currentSummDelivered(endpoint, {min: 10, max: 300, change: [0, 1]});
    },
};

module.exports = definition;
