package com.quant.api.service;

import com.quant.api.dto.CommandRequest;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestTemplate;

import java.util.Map;

@Slf4j
@Service
@RequiredArgsConstructor
public class PythonEngineClient {

    private final RestTemplate restTemplate;

    @Value("${python.engine.base-url}")
    private String baseUrl;

    public Map<?, ?> triggerAnalyze(CommandRequest req) {
        log.info("Python /analyze 호출: market={} horizon={} topN={} lookbackDays={}",
            req.getMarket(), req.getHorizon(), req.getTopN(), req.getLookbackDays());
        var body = new java.util.HashMap<String, Object>();
        body.put("market", req.getMarket());
        body.put("horizon", req.getHorizon() != null ? req.getHorizon() : "swing");
        body.put("top_n", req.getTopN() != null ? req.getTopN() : 10);
        if (req.getLookbackDays() != null) {
            body.put("lookback_days", req.getLookbackDays());
        }
        try {
            ResponseEntity<Map> resp = restTemplate.postForEntity(baseUrl + "/analyze", body, Map.class);
            return resp.getBody();
        } catch (org.springframework.web.client.HttpClientErrorException e) {
            // FastAPI 4xx (409 이미 실행 중 등)를 그대로 클라이언트에 전달
            String detail = extractDetail(e.getResponseBodyAsString());
            throw new org.springframework.web.server.ResponseStatusException(e.getStatusCode(), detail);
        }
    }

    private String extractDetail(String body) {
        try {
            var node = new com.fasterxml.jackson.databind.ObjectMapper().readTree(body);
            return node.path("detail").asText(body);
        } catch (Exception e) {
            return body;
        }
    }

    public Map<?, ?> triggerBacktest(CommandRequest req) {
        log.info("Python /backtest 호출: code={} market={} start={} end={}",
            req.getStockCode(), req.getMarket(), req.getStartDate(), req.getEndDate());
        var body = new java.util.HashMap<String, Object>();
        body.put("stock_code", req.getStockCode());
        body.put("market",     req.getMarket());
        if (req.getStartDate() != null) {
            body.put("start_date", req.getStartDate());
        } else {
            body.put("period_days", req.getPeriodDays() != null ? req.getPeriodDays() : 60);
        }
        if (req.getEndDate() != null) {
            body.put("end_date", req.getEndDate());
        }
        ResponseEntity<Map> resp = restTemplate.postForEntity(baseUrl + "/backtest", body, Map.class);
        return resp.getBody();
    }

    public Map<?, ?> startTrading(CommandRequest req) {
        log.info("Python /trade/start 호출: market={} mode={}", req.getMarket(), req.getMode());
        var body = new java.util.HashMap<String, Object>();
        body.put("market", req.getMarket());
        if (req.getMode() != null) {
            body.put("mode", req.getMode());
        }
        try {
            ResponseEntity<Map> resp = restTemplate.postForEntity(baseUrl + "/trade/start", body, Map.class);
            return resp.getBody();
        } catch (org.springframework.web.client.HttpClientErrorException e) {
            String detail = extractDetail(e.getResponseBodyAsString());
            throw new org.springframework.web.server.ResponseStatusException(e.getStatusCode(), detail);
        }
    }

    public Map<?, ?> stopTrading() {
        log.info("Python /trade/stop 호출");
        ResponseEntity<Map> resp = restTemplate.postForEntity(baseUrl + "/trade/stop", null, Map.class);
        return resp.getBody();
    }

    public Map<?, ?> setMode(CommandRequest req) {
        log.info("Python /mode 호출: mode={}", req.getMode());
        Map<String, Object> body = Map.of("mode", req.getMode());
        try {
            ResponseEntity<Map> resp = restTemplate.postForEntity(baseUrl + "/mode", body, Map.class);
            return resp.getBody();
        } catch (org.springframework.web.client.HttpClientErrorException e) {
            String detail = extractDetail(e.getResponseBodyAsString());
            throw new org.springframework.web.server.ResponseStatusException(e.getStatusCode(), detail);
        }
    }

    public Map<?, ?> health() {
        ResponseEntity<Map> resp = restTemplate.getForEntity(baseUrl + "/health", Map.class);
        return resp.getBody();
    }

    public Map<?, ?> getAccountBalance(String market, String mode) {
        String url = baseUrl + "/account/balance?market=" + market;
        if (mode != null && !mode.isBlank()) url += "&mode=" + mode;
        ResponseEntity<Map> resp = restTemplate.getForEntity(url, Map.class);
        return resp.getBody();
    }

    public Object getLivePositions(String mode) {
        ResponseEntity<Object> resp = restTemplate.getForEntity(
            baseUrl + "/trade/positions/live?mode=" + mode, Object.class);
        return resp.getBody();
    }

    public Object getPendingOrders(String mode) {
        ResponseEntity<Object> resp = restTemplate.getForEntity(
            baseUrl + "/trade/orders/pending?mode=" + mode, Object.class);
        return resp.getBody();
    }

    public Map<?, ?> analyzeProgress(Long runId) {
        ResponseEntity<Map> resp = restTemplate.getForEntity(
            baseUrl + "/analyze/" + runId + "/progress", Map.class);
        return resp.getBody();
    }

    public Map<?, ?> getRegime() {
        ResponseEntity<Map> resp = restTemplate.getForEntity(baseUrl + "/regime", Map.class);
        return resp.getBody();
    }

    public Object getSignals() {
        ResponseEntity<Object> resp = restTemplate.getForEntity(baseUrl + "/signals", Object.class);
        return resp.getBody();
    }
}
