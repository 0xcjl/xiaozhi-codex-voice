package xiaozhi.modules.connection.dto;

import jakarta.validation.constraints.*;

public record ConnectionBody(
    @NotBlank @Size(max = 80) String name,
    @NotNull @Pattern(regexp = "api|chatgpt_realtime") String kind,
    @Size(max = 64) String modelId,
    @Size(max = 64) String runtimeId,
    @Size(max = 80) String model,
    @NotNull Boolean enabled,
    @NotNull @Min(0) Integer revision
) {}
