package com.example.snmppolling.service;

import org.snmp4j.CommunityTarget;
import org.snmp4j.PDU;
import org.snmp4j.Snmp;
import org.snmp4j.TransportMapping;
import org.snmp4j.event.ResponseEvent;
import org.snmp4j.mp.SnmpConstants;
import org.snmp4j.smi.*;
import org.snmp4j.transport.DefaultUdpTransportMapping;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestTemplate;
import org.springframework.http.ResponseEntity;
import org.springframework.http.HttpStatus;

import java.io.IOException;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

@Service
public class SnmpKmxService {

    private static final Logger logger = LoggerFactory.getLogger(SnmpKmxService.class);

    // GSM Status Map from MIRANDA-MIB (assuming values are integers)
    private static final Map<Integer, String> GSM_STATUS_MAP = new HashMap<>();
    static {
        GSM_STATUS_MAP.put(-1, "disabled");
        GSM_STATUS_MAP.put(10000, "normal");
        GSM_STATUS_MAP.put(20000, "minor/warning");
        GSM_STATUS_MAP.put(25000, "major");
        GSM_STATUS_MAP.put(30000, "critical/error");
        GSM_STATUS_MAP.put(40000, "unknown");
    }

    // --- Configuration values injected from application.properties ---
    @Value("${kmx.snmp.ip:172.19.185.230}")
    private String kmxIp;

    @Value("${kmx.snmp.community:public}")
    private String kmxCommunity;

    @Value("${kmx.snmp.baseOid:.1.3.6.1.4.1.3872.21.6.2.1}")
    private String kmxBaseOid; // Corrected: kmxBaseOid (capital 'O')

    @Value("${flask.backend.url:http://localhost:5000/api/v1/update_kmx_data}")
    private String flaskBackendUrl;

    private final RestTemplate restTemplate;

    public SnmpKmxService(RestTemplate restTemplate) {
        this.restTemplate = restTemplate;
    }

    private String resolveOidName(String oid) {
        return oid;
    }

    /**
     * Helper method to perform a single SNMP GET operation.
     * Returns a map with 'value', 'interpreted', and 'status'.
     */
    private Map<String, String> getSnmpValue(String ip, String community, String oid) throws IOException {
        TransportMapping<UdpAddress> transport = null;
        Snmp snmp = null;
        Map<String, String> result = new HashMap<>();
        result.put("status", "Failed");

        try {
            transport = new DefaultUdpTransportMapping();
            transport.listen();
            snmp = new Snmp(transport);

            CommunityTarget target = new CommunityTarget();
            target.setCommunity(new OctetString(community));
            target.setAddress(new UdpAddress(ip + "/161"));
            target.setRetries(2);
            target.setTimeout(1000);
            target.setVersion(SnmpConstants.version2c);

            PDU pdu = new PDU();
            pdu.add(new VariableBinding(new OID(oid)));
            pdu.setType(PDU.GET);

            ResponseEvent event = snmp.get(pdu, target);

            if (event == null || event.getResponse() == null) {
                result.put("value", "No Response");
                result.put("interpreted", "N/A");
                result.put("status", "No Response");
            } else {
                VariableBinding vb = event.getResponse().get(0);
                if (vb.getVariable() instanceof Null) {
                    result.put("value", "No Such Object");
                    result.put("interpreted", "N/A");
                    result.put("status", "Error");
                } else {
                    String rawValue = vb.getVariable().toString();
                    String interpretedValue = rawValue;
                    try {
                        int intValue = Integer.parseInt(rawValue);
                        interpretedValue = GSM_STATUS_MAP.getOrDefault(intValue, rawValue);
                    } catch (NumberFormatException e) {
                        // Not an integer, keep raw value as interpreted
                    }
                    result.put("value", rawValue);
                    result.put("interpreted", interpretedValue);
                    result.put("status", "Success");
                }
            }
        } catch (Exception e) {
            logger.error("Error in getSnmpValue for {}: OID {}: {}", ip, oid, e.getMessage());
            result.put("value", "Error: " + e.getMessage());
            result.put("interpreted", "N/A");
            result.put("status", "Error");
        } finally {
            if (snmp != null) {
                try { snmp.close(); } catch (IOException e) { logger.warn("Error closing SNMP session: {}", e.getMessage()); }
            }
            if (transport != null) {
                try { transport.close(); } catch (IOException e) { logger.warn("Error closing SNMP transport: {}", e.getMessage()); }
            }
        }
        return result;
    }

    /**
     * Performs a filtered SNMP walk for the KMX device (fixed IP).
     * This method is scheduled to run periodically.
     */
    @Scheduled(fixedRate = 60000)
    public void fetchAndPushKmxSnmpData() {
        logger.info("Starting KMX SNMP data fetch for IP: {}", kmxIp);
        List<Map<String, String>> filteredAlarms = new ArrayList<>();
        String overallKmxStatus = "ok";

        TransportMapping<UdpAddress> transport = null;
        Snmp snmp = null;
        Set<String> childIds = new HashSet<>();

        try {
            transport = new DefaultUdpTransportMapping();
            transport.listen();
            snmp = new Snmp(transport);

            CommunityTarget target = new CommunityTarget();
            target.setCommunity(new OctetString(kmxCommunity));
            target.setAddress(new UdpAddress(kmxIp + "/161"));
            target.setRetries(2);
            target.setTimeout(1000);
            target.setVersion(SnmpConstants.version2c);

            // 1. Walk the severity OID branch to find all child IDs
            OID severityBranchOid = new OID(kmxBaseOid + ".3");
            OID nextOid = severityBranchOid;

            while (true) {
                PDU pdu = new PDU();
                pdu.add(new VariableBinding(nextOid));
                pdu.setType(PDU.GETNEXT);

                ResponseEvent event = snmp.getNext(pdu, target);
                if (event == null || event.getResponse() == null) {
                    logger.warn("SNMP Walk stopped: No response or empty response for OID {}", nextOid);
                    break;
                }

                VariableBinding vb = event.getResponse().get(0);
                if (vb.getOid() == null || !vb.getOid().startsWith(severityBranchOid) || vb.getOid().equals(nextOid)) {
                    break;
                }

                String fullOid = vb.getOid().toString();
                String childId = fullOid.substring(fullOid.lastIndexOf('.') + 1);
                childIds.add(childId);
                nextOid = vb.getOid();
                logger.debug("Discovered child ID: {} from OID: {}", childId, fullOid);
            }

            // 2. For each collected child ID, perform specific GETs and apply filters
            for (String childId : childIds) {
                String currentSeverityOid = kmxBaseOid + ".3." + childId;
                String currentDeviceNameOid = kmxBaseOid + ".2." + childId; // Corrected here
                String currentActualValueOid = kmxBaseOid + ".5." + childId;

                Map<String, String> severityResult = getSnmpValue(kmxIp, kmxCommunity, currentSeverityOid);

                // --- APPLY SEVERITY FILTERING ---
                if ("10000".equals(severityResult.get("value"))) {
                    logger.debug("Skipping childId {} (Severity OK: 10000).", childId);
                    continue;
                }

                Map<String, String> deviceNameResult = getSnmpValue(kmxIp, kmxCommunity, currentDeviceNameOid);
                String deviceName = deviceNameResult.get("interpreted");

                // --- APPLY DEVICE NAME FILTERING ---
                if (deviceName != null && deviceName.contains("HCO")) {
                    logger.debug("Skipping childId {} (Device Name contains 'HCO': {}).", childId, deviceName);
                    continue;
                }

                Map<String, String> actualValueResult = getSnmpValue(kmxIp, kmxCommunity, currentActualValueOid);
                String rawActualValue = actualValueResult.get("value");

                // --- APPLY ACTUAL VALUE FILTERING ---
                String displayMessage = rawActualValue;
                if ("source 23;source 23".equals(rawActualValue)) {
                    displayMessage = "";
                }

                // Determine alarm severity for console and overall block status
                String alarmConsoleSeverity = "UNKNOWN";
                String interpretedSeverityText = severityResult.get("interpreted");

                if (displayMessage.toUpperCase().contains("VIDEO BLACK") || displayMessage.toUpperCase().contains("VIDEO FREEZE")) {
                    alarmConsoleSeverity = "CRITICAL";
                } else if (displayMessage.toUpperCase().contains("SILENCE LEFT") || displayMessage.toUpperCase().contains("SILENCE RIGHT")) {
                    if (!"CRITICAL".equals(alarmConsoleSeverity)) {
                        alarmConsoleSeverity = "WARNING";
                    }
                } else if ("CRITICAL/ERROR".equals(interpretedSeverityText.toUpperCase())) {
                    alarmConsoleSeverity = "CRITICAL";
                } else if ("MINOR/WARNING".equals(interpretedSeverityText.toUpperCase())) {
                    if (!"CRITICAL".equals(alarmConsoleSeverity)) {
                        alarmConsoleSeverity = "WARNING";
                    }
                }
                
                // Update overall KMX status for the block
                if ("CRITICAL".equals(alarmConsoleSeverity)) {
                    overallKmxStatus = "alarm";
                } else if ("WARNING".equals(alarmConsoleSeverity) && !"alarm".equals(overallKmxStatus)) {
                    overallKmxStatus = "warning";
                }

                Map<String, String> alarmEntry = new HashMap<>();
                alarmEntry.put("server_ip", kmxIp);
                alarmEntry.put("device_name", deviceName);
                alarmEntry.put("message", displayMessage);
                alarmEntry.put("severity", alarmConsoleSeverity);
                alarmEntry.put("timestamp", Instant.now().toString());
                alarmEntry.put("type", "snmp_kmx");
                alarmEntry.put("oid", currentSeverityOid);

                filteredAlarms.add(alarmEntry);
                logger.info("Filtered KMX alarm added: DeviceName={}, Severity={}, Message='{}'", deviceName, alarmConsoleSeverity, displayMessage);
            }

        } catch (IOException e) {
            logger.error("IO Error during KMX SNMP walk for {}: {}", kmxIp, e.getMessage());
            overallKmxStatus = "alarm";
            Map<String, String> errorAlarm = new HashMap<>();
            errorAlarm.put("server_ip", kmxIp);
            errorAlarm.put("device_name", "KMX Monitor (Java)");
            errorAlarm.put("message", "Failed to connect to SNMP agent: " + e.getMessage());
            errorAlarm.put("severity", "CRITICAL");
            errorAlarm.put("timestamp", Instant.now().toString());
            errorAlarm.put("type", "snmp_kmx_service_error");
            filteredAlarms.add(errorAlarm);
        } catch (Exception e) {
            logger.error("Unexpected error during KMX SNMP walk for {}: {}", kmxIp, e.getMessage());
            overallKmxStatus = "alarm";
            Map<String, String> errorAlarm = new HashMap<>();
            errorAlarm.put("server_ip", kmxIp);
            errorAlarm.put("device_name", "KMX Monitor (Java)");
            errorAlarm.put("message", "Unexpected error: " + e.getMessage());
            errorAlarm.put("severity", "CRITICAL");
            errorAlarm.put("timestamp", Instant.now().toString());
            errorAlarm.put("type", "snmp_kmx_service_error");
            filteredAlarms.add(errorAlarm);
        } finally {
            if (snmp != null) {
                try { snmp.close(); } catch (IOException e) { logger.warn("Error closing SNMP session: {}", e.getMessage()); }
            }
            if (transport != null) {
                try { transport.close(); } catch (IOException e) { logger.warn("Error closing SNMP transport: {}", e.getMessage()); }
            }
        }

        Map<String, Object> payload = new HashMap<>();
        payload.put("overall_status", overallKmxStatus);
        payload.put("alarms", filteredAlarms);

        sendDataToFlaskBackend(payload);
    }

    private void sendDataToFlaskBackend(Map<String, Object> data) {
        try {
            ResponseEntity<String> response = restTemplate.postForEntity(flaskBackendUrl, data, String.class);
            if (response.getStatusCode() == HttpStatus.OK) {
                logger.info("Successfully pushed KMX SNMP data to Flask backend.");
            } else {
                logger.error("Failed to push KMX SNMP data to Flask backend. Status: {}, Body: {}", response.getStatusCode(), response.getBody());
            }
        } catch (Exception e) {
            logger.error("Error sending KMX SNMP data to Flask backend: {}", e.getMessage());
            logger.debug("Payload that failed to send: {}", data);
        }
    }
}
