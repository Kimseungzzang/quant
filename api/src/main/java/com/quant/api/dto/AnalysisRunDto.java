package com.quant.api.dto;

import com.quant.api.domain.AnalysisRun;
import lombok.Builder;
import lombok.Getter;

import java.time.OffsetDateTime;

@Getter
@Builder
public class AnalysisRunDto {
    private Long id;
    private String market;
    private String horizon;
    private OffsetDateTime runAt;
    private String status;
    private int resultCount;

    public static AnalysisRunDto from(AnalysisRun run) {
        return AnalysisRunDto.builder()
            .id(run.getId())
            .market(run.getMarket())
            .horizon(run.getHorizon())
            .runAt(run.getRunAt())
            .status(run.getStatus())
            .resultCount(run.getResults().size())
            .build();
    }
}
