package com.quant.api.controller;

import com.quant.api.dto.CommandRequest;
import com.quant.api.service.PythonEngineClient;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.*;

import java.util.Map;

@RestController
@RequestMapping("/api/command")
@RequiredArgsConstructor
@CrossOrigin(origins = {"http://localhost:3000"})
public class CommandController {

    private final PythonEngineClient pythonEngineClient;

    @PostMapping("/analyze")
    public Map<?, ?> analyze(@RequestBody(required = false) CommandRequest req) {
        return pythonEngineClient.triggerAnalyze(req != null ? req : new CommandRequest());
    }

    @PostMapping("/backtest")
    public Map<?, ?> backtest(@RequestBody CommandRequest req) {
        return pythonEngineClient.triggerBacktest(req);
    }

    @PostMapping("/trade/start")
    public Map<?, ?> tradeStart(@RequestBody(required = false) CommandRequest req) {
        return pythonEngineClient.startTrading(req != null ? req : new CommandRequest());
    }

    @PostMapping("/trade/stop")
    public Map<?, ?> tradeStop() {
        return pythonEngineClient.stopTrading();
    }

    @GetMapping("/health")
    public Map<?, ?> engineHealth() {
        return pythonEngineClient.health();
    }

    @GetMapping("/analyze/{runId}/progress")
    public Map<?, ?> analyzeProgress(@PathVariable Long runId) {
        return pythonEngineClient.analyzeProgress(runId);
    }

    @GetMapping("/regime")
    public Map<?, ?> getRegime() {
        return pythonEngineClient.getRegime();
    }
}
