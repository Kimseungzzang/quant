package com.quant.api.controller;

import com.quant.api.dto.AnalysisResultDto;
import com.quant.api.dto.AnalysisRunDto;
import com.quant.api.service.AnalysisService;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/analysis")
@RequiredArgsConstructor
@CrossOrigin(origins = {"http://localhost:3002"})
public class AnalysisController {

    private final AnalysisService analysisService;

    @GetMapping("/runs")
    public List<AnalysisRunDto> getRuns(
        @RequestParam(defaultValue = "domestic") String market,
        @RequestParam(defaultValue = "swing") String horizon
    ) {
        return analysisService.getRuns(market, horizon);
    }

    @GetMapping
    public List<AnalysisResultDto> getLatest(
        @RequestParam(defaultValue = "domestic") String market,
        @RequestParam(defaultValue = "swing") String horizon
    ) {
        return analysisService.getLatest(market, horizon);
    }

    @GetMapping("/running")
    public AnalysisRunDto getRunning(
        @RequestParam(defaultValue = "domestic") String market,
        @RequestParam(defaultValue = "swing") String horizon
    ) {
        return analysisService.getRunningRun(market, horizon);
    }

    @GetMapping("/run/{runId}")
    public List<AnalysisResultDto> getByRun(@PathVariable Long runId) {
        return analysisService.getByRunId(runId);
    }
}
