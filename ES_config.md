## ES index monitor_historical_alarms 
```
curl -X PUT "http://192.168.56.30:9200/monitor_historical_alarms" -H "Content-Type: application/json" -d'
{
  "settings": {
    "number_of_shards": 3,
    "number_of_replicas": 1
  },
  "mappings": {
    "properties": {
      "alarm_id": { "type": "keyword" },
      "timestamp": { "type": "date" },
      "message": { "type": "text" },
      "device_name": { "type": "keyword" },
      "block_id": { "type": "keyword" },
      "severity": { "type": "keyword" },
      "type": { "type": "keyword" },
      "device_ip": { "type": "ip" },
      "group_name": { "type": "keyword" }
    }
  }
}'
```
## ES index ird_trend_data

```
curl -X PUT "http://192.168.56.30:9200/ird_trend_data" -H "Content-Type: application/json" -d'
{
  "settings": {
    "number_of_shards": 3,
    "number_of_replicas": 1
  },
  "mappings": {
    "properties": {
      "channel_name": { "type": "keyword" },
      "system_id": { "type": "integer" },
      "C_N_margin": { "type": "float" },
      "signal_strength": { "type": "float" },
      "timestamp": { "type": "date" },
      "ip_address": { "type": "ip" }
    }
  }
}'
```
## ES index ird_configurations 
```
curl -X PUT "http://192.168.56.30:9200/ird_configurations" -H "Content-Type: application/json" -d'
{
  "settings": {
    "number_of_shards": 1,
    "number_of_replicas": 0
  },
  "mappings": {
    "properties": {
      "ird_ip": { "type": "keyword" },
      "channel_name": { "type": "keyword" },
      "system_id": { "type": "integer" },
      "freq": { "type": "float" },
      "SR": { "type": "float" },
      "Pol": { "type": "keyword" },
      "C_N": { "type": "float" },
      "signal_strength": { "type": "float" },
      "moip_status": { "type": "keyword" },
      "output_bitrate": { "type": "float" },
      "input_bitrate": { "type": "float" },
      "last_updated": { "type": "date" }
    }
  }
}'
```

```
curl -X PUT "http://192.168.56.30:9200/switch_overview" -H 'Content-Type: application/json' -d '{
  "mappings": {
    "properties": {
      "switch_ip": { "type": "keyword" },
      "hostname": { "type": "keyword" },
      "interfaces": {
        "type": "nested",
        "properties": {
          "name": { "type": "keyword" },
          "alias": { "type": "text" },
          "admin_status": { "type": "keyword" },
          "oper_status": { "type": "keyword" },
          "vlan": { "type": "integer" },
          "input_octets": { "type": "long" },
          "output_octets": { "type": "long" }
        }
      },
      "timestamp": { "type": "date" }
    }
  }
}'
```