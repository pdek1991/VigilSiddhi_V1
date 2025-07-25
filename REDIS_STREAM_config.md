# Agent-specific processor groups
XGROUP CREATE vs:agent:cisco_sw_status vs:group:cisco_sw_processor $ MKSTREAM
XGROUP CREATE vs:agent:enc_ilo_status vs:group:enc_ilo_processor $ MKSTREAM
XGROUP CREATE vs:agent:iloM_status vs:group:iloM_processor $ MKSTREAM
XGROUP CREATE vs:agent:iloP_status vs:group:iloP_processor $ MKSTREAM
XGROUP CREATE vs:agent:iloB_status vs:group:iloB_processor $ MKSTREAM
XGROUP CREATE vs:agent:playoutmv_status vs:group:playoutmv_processor $ MKSTREAM
XGROUP CREATE vs:agent:windows_status vs:group:windows_processor $ MKSTREAM
XGROUP CREATE vs:agent:ird_config_status vs:group:ird_config_processor $ MKSTREAM
XGROUP CREATE vs:agent:ird_trend_status vs:group:ird_trend_processor $ MKSTREAM
XGROUP CREATE vs:agent:zixi_status vs:group:zixi_processor $ MKSTREAM
XGROUP CREATE vs:agent:nexus_sw_status vs:group:nexus_sw_processor $ MKSTREAM
XGROUP CREATE vs:agent:gv_da_status vs:group:gv_da_processor $ MKSTREAM
XGROUP CREATE vs:agent:pgm_router_status vs:group:pgm_router_processor $ MKSTREAM
XGROUP CREATE vs:agent:kmx_status vs:group:kmx_processor $ MKSTREAM

# Central consumers (websocket and Elasticsearch)
XGROUP CREATE vs:agent:cisco_sw_status vs:group:websocket_broadcaster $ MKSTREAM
XGROUP CREATE vs:agent:enc_iloM_status vs:group:websocket_broadcaster $ MKSTREAM
XGROUP CREATE vs:agent:enc_iloP_status vs:group:websocket_broadcaster $ MKSTREAM
XGROUP CREATE vs:agent:enc_iloB_status vs:group:websocket_broadcaster $ MKSTREAM
XGROUP CREATE vs:agent:iloM_status vs:group:websocket_broadcaster $ MKSTREAM
XGROUP CREATE vs:agent:iloP_status vs:group:websocket_broadcaster $ MKSTREAM
XGROUP CREATE vs:agent:iloB_status vs:group:websocket_broadcaster $ MKSTREAM
XGROUP CREATE vs:agent:playoutmv_status vs:group:websocket_broadcaster $ MKSTREAM
XGROUP CREATE vs:agent:windows_status vs:group:websocket_broadcaster $ MKSTREAM
XGROUP CREATE vs:agent:ird_config_status vs:group:websocket_broadcaster $ MKSTREAM
XGROUP CREATE vs:agent:ird_trend_status vs:group:websocket_broadcaster $ MKSTREAM
XGROUP CREATE vs:agent:zixi_status vs:group:websocket_broadcaster $ MKSTREAM
XGROUP CREATE vs:agent:nexus_sw_status vs:group:websocket_broadcaster $ MKSTREAM
XGROUP CREATE vs:agent:gv_da_status vs:group:websocket_broadcaster $ MKSTREAM
XGROUP CREATE vs:agent:pgm_router_status vs:group:websocket_broadcaster $ MKSTREAM
XGROUP CREATE vs:agent:kmx_status vs:group:websocket_broadcaster $ MKSTREAM

XGROUP CREATE vs:agent:cisco_sw_status vs:group:es_ingester $ MKSTREAM
XGROUP CREATE vs:agent:enc_ilo_status vs:group:es_ingester $ MKSTREAM
XGROUP CREATE vs:agent:iloM_status vs:group:es_ingester $ MKSTREAM
XGROUP CREATE vs:agent:iloP_status vs:group:es_ingester $ MKSTREAM
XGROUP CREATE vs:agent:iloB_status vs:group:es_ingester $ MKSTREAM
XGROUP CREATE vs:agent:playoutmv_status vs:group:es_ingester $ MKSTREAM
XGROUP CREATE vs:agent:windows_status vs:group:es_ingester $ MKSTREAM
XGROUP CREATE vs:agent:ird_config_status vs:group:es_ingester $ MKSTREAM
XGROUP CREATE vs:agent:ird_trend_status vs:group:es_ingester $ MKSTREAM
XGROUP CREATE vs:agent:zixi_status vs:group:es_ingester $ MKSTREAM
XGROUP CREATE vs:agent:nexus_sw_status vs:group:es_ingester $ MKSTREAM
XGROUP CREATE vs:agent:gv_da_status vs:group:es_ingester $ MKSTREAM
XGROUP CREATE vs:agent:pgm_router_status vs:group:es_ingester $ MKSTREAM
XGROUP CREATE vs:agent:kmx_status vs:group:es_ingester $ MKSTREAM