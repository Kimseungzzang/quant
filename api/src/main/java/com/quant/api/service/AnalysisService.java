package com.quant.api.service;

import com.quant.api.dto.AnalysisResultDto;
import com.quant.api.dto.AnalysisRunDto;
import com.quant.api.repository.AnalysisResultRepository;
import com.quant.api.repository.AnalysisRunRepository;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

@Service
@RequiredArgsConstructor
@Transactional(readOnly = true)
public class AnalysisService {

    private final AnalysisResultRepository analysisResultRepository;
    private final AnalysisRunRepository analysisRunRepository;

    public List<AnalysisRunDto> getRuns(String market, String horizon) {
        return analysisRunRepository.findByMarketAndHorizonAndStatusOrderByRunAtDesc(market, horizon, "completed")
            .stream()
            .map(AnalysisRunDto::from)
            .toList();
    }

    public List<AnalysisResultDto> getLatest(String market, String horizon) {
        return analysisResultRepository.findLatestByMarketAndHorizon(market, horizon)
            .stream()
            .map(AnalysisResultDto::from)
            .toList();
    }

    public AnalysisRunDto getRunningRun(String market, String horizon) {
        return analysisRunRepository.findByMarketAndHorizonAndStatusOrderByIdDesc(market, horizon, "running")
            .stream().findFirst().map(AnalysisRunDto::from).orElse(null);
    }

    public List<AnalysisResultDto> getByRunId(Long runId) {
        return analysisResultRepository.findByRunId(runId)
            .stream()
            .map(AnalysisResultDto::from)
            .toList();
    }
}
