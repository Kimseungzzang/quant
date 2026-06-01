package com.quant.api.controller;

import com.quant.api.domain.BacktestResult;
import com.quant.api.service.BacktestService;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/backtest")
@RequiredArgsConstructor
@CrossOrigin(origins = {"http://localhost:3002"})
public class BacktestController {

    private final BacktestService backtestService;

    @GetMapping
    public List<BacktestResult> getByMarket(
        @RequestParam(defaultValue = "domestic") String market,
        @RequestParam(defaultValue = "20") int limit
    ) {
        return backtestService.getByMarket(market, limit);
    }

    @GetMapping("/stock/{code}")
    public List<BacktestResult> getByStock(
        @PathVariable String code,
        @RequestParam(defaultValue = "10") int limit
    ) {
        return backtestService.getByStockCode(code, limit);
    }

    @GetMapping("/run/{runId}")
    public List<BacktestResult> getByRun(@PathVariable Long runId) {
        return backtestService.getByAnalysisRun(runId);
    }
}
